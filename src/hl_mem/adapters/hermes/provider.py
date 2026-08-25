"""保持 Hermes hook 契约稳定的 HL-Mem 协调适配器。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from hl_mem.adapters.hermes.conflict_notice import ManualConflictNotice
from hl_mem.adapters.hermes.episode_mapper import EpisodeMapper
from hl_mem.adapters.hermes.http_client import HLMemHttpClient
from hl_mem.adapters.hermes.prefetch import PrefetchCache
from hl_mem.adapters.hermes.renderer import render_context
from hl_mem.application.context_packet import (
    RetrievalBundle,
    UnknownSchemaMajorError,
    retrieval_bundle_to_dict,
)
from hl_mem.compatibility import CONTEXT_PACKET_SCHEMA_MAJOR
from hl_mem.http_utils import validation_response_body
from hl_mem.recall.injection import DeliveryPurpose, InjectionContext
from hl_mem.settings import Settings

logger = logging.getLogger(__name__)

MAX_TRACE_ACTION_LENGTH = 10_000
MAX_TRACE_OBSERVATION_SUMMARY_LENGTH = 500
MAX_EPISODE_GOAL_LENGTH = 5_000
MAX_EPISODE_ERROR_BODY_LENGTH = 1_000
EPISODE_GOAL_FALLBACK = "Complete tool-assisted task"
MAX_DELIVERY_RECEIPTS = 128
MAX_INJECTION_ATTEMPTS = 3
HERMES_RECALL_TOOL_NAME = "hl_mem_recall"
HERMES_RECALL_INTENTS = ("current_state", "historical", "preference", "tool", "procedure")
HERMES_RECALL_TOOL_DESCRIPTION = (
    "查询 hl_mem 长期记忆库中的历史事实。何时用我：部署、升级或运维前，先查目标机器的部署历史和已知状态；"
    "遇到端口占用、版本不符、配置异常等环境意外时，查环境已知事实；需要历史决策及原因时查询。"
    "当前对话已注入的记忆足够时，不必重复调用。 Search historical facts in hl_mem long-term memory. "
    "When to use: before deployment, upgrade, or operations, check the target machine's deployment history and "
    "known state; when surprises such as occupied ports, version mismatch, or abnormal configuration appear, check "
    "known environment facts; when prior decisions or rationale are needed. Skip when memories already injected into "
    "the current conversation are sufficient."
)
_ERROR_PATTERNS = (
    re.compile(r"^Traceback", re.MULTILINE),
    re.compile(r"^Error:", re.MULTILINE),
    re.compile(r"^FAILED\b", re.MULTILINE),
    re.compile(r"\bException\b"),
    re.compile(r"\b(?:[A-Za-z_]\w*)?Error\b(?:[ \t]+[^:\r\n]+)?:"),
)
_EXIT_CODE_PATTERN = re.compile(r'["\']?exit_code["\']?\s*[:=]\s*(-?\d+)')


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Hermes 内部 delivery 记录；不进入 Context Packet wire schema。"""

    session: str
    turn: int | str
    query_id: str
    feedback_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PendingInjection:
    receipt: DeliveryReceipt
    attempts: int


def _summarize_observation(raw: str) -> str:
    """生成结构化摘要替代完整原文。"""
    if not raw:
        return ""
    exit_codes = (int(match.group(1)) for match in _EXIT_CODE_PATTERN.finditer(raw))
    is_error = any(pattern.search(raw) for pattern in _ERROR_PATTERNS) or any(code != 0 for code in exit_codes)
    status = "error" if is_error else "success"
    summary = raw[:MAX_TRACE_OBSERVATION_SUMMARY_LENGTH].strip()
    return f"[{status}] {summary}"


def _memory_idempotency_key(
    key: str,
    target: str,
    content: str,
    namespace: str = "default",
) -> str:
    """从 Hermes host identity 与正文摘要生成稳定、无正文的重试键。"""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = json.dumps(
        [namespace, key, target, content_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"hermes-memory:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _trusted_namespace(namespace: str) -> str:
    """Validate a namespace supplied by trusted host configuration or hook arguments."""
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be a non-empty string")
    if len(namespace) > 100:
        raise ValueError("namespace must be at most 100 characters")
    return namespace


def _episode_goal(content: str) -> str:
    """把本轮 user content 收敛到 EpisodeInput 契约。"""
    return (content.strip() or EPISODE_GOAL_FALLBACK)[:MAX_EPISODE_GOAL_LENGTH]


def _messages_after_last_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留本轮最后一条 user 消息之后的轨迹。"""
    last_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"),
        None,
    )
    return messages if last_user_index is None else messages[last_user_index + 1 :]


def _validation_response_body(response: httpx.Response) -> str:
    """保留 422 诊断结构，但移除可能包含完整对话的 input。"""
    return validation_response_body(response, limit=MAX_EPISODE_ERROR_BODY_LENGTH)


class HLMemProvider:
    """Hermes 兼容协调层；HTTP、缓存与 Episode 映射委托给独立组件。"""

    def __init__(
        self,
        db_path: str | None = None,
        daemon_url: str | None = None,
        timeout: float | None = None,
        *,
        settings: Settings | None = None,
        enabled: bool | None = None,
        hermes_home: str | None = None,
    ) -> None:
        resolved_settings = settings or Settings()
        self.settings = resolved_settings
        self.db_path = db_path
        configured_daemon_url = daemon_url or resolved_settings.hermes_url
        if configured_daemon_url is None:
            raise ValueError("daemon URL must be configured")
        configured_timeout = timeout if timeout is not None else float(resolved_settings.hermes_timeout)
        if configured_timeout <= 0:
            raise ValueError("timeout must be positive")
        self.enabled = resolved_settings.hermes_enabled if enabled is None else enabled
        self.daemon_url = configured_daemon_url.rstrip("/")
        self.timeout = configured_timeout
        self._client = HLMemHttpClient(
            self.daemon_url,
            configured_timeout,
            resolved_settings.hermes_circuit_failure_threshold,
            resolved_settings.hermes_circuit_open_seconds,
        )
        self._prefetch_cache = PrefetchCache(
            self._client,
            resolved_settings.hermes_prefetch_cache_ttl_seconds,
            projection_version=(f"{resolved_settings.index_text_mode}:" f"{resolved_settings.index_text_version}"),
            on_demand_timeout_seconds=resolved_settings.hermes_on_demand_recall_timeout_seconds,
        )
        self._mapper = EpisodeMapper()
        self._session_id = ""
        self._hermes_home = hermes_home or resolved_settings.hermes_home or ""
        self._delivery_lock = threading.Lock()
        self._delivery_receipts: deque[DeliveryReceipt] = deque(maxlen=MAX_DELIVERY_RECEIPTS)
        self._pending_injections: deque[_PendingInjection] = deque()
        self._session_turns: dict[str, int] = {}
        self._recall_tool_calls = 0
        self._conflict_notice = ManualConflictNotice(
            self.enabled and resolved_settings.hermes_manual_conflict_notice,
            resolved_settings.hermes_prefetch_cache_ttl_seconds,
        )
        self._delivery_health: dict[str, int | str | None] = {
            "deliveries": 0,
            "bundle_misses": 0,
            "materialization_failures": 0,
            "injection_failures": 0,
            "injection_successes": 0,
            "injection_deferred": 0,
            "injection_retries": 0,
            "injection_abandoned": 0,
            "schema_failures": 0,
            "last_error": None,
        }

    @property
    def name(self) -> str:
        """返回 Hermes 使用的提供器名称。"""
        return "hl_mem"

    @property
    def state(self) -> str:
        """返回只读熔断状态：open、closed 或 half_open。"""
        return self._client.state

    @property
    def delivery_receipts(self) -> tuple[DeliveryReceipt, ...]:
        """返回有界、无正文的 delivery receipt 历史快照。"""
        with self._delivery_lock:
            return tuple(self._delivery_receipts)

    def health(self) -> dict[str, Any]:
        """返回 Hermes prefetch/delivery 的无敏感内容健康快照。"""
        prefetch = self._prefetch_cache.health()
        with self._delivery_lock:
            delivery = dict(self._delivery_health)
            delivery["pending_injections"] = len(self._pending_injections)
            delivery["retained_receipts"] = len(self._delivery_receipts)
            tool_calls = self._recall_tool_calls
        return {
            "circuit_state": self.state,
            "prefetch_failures": int(prefetch["retrieval_failures"] or 0),
            "injection_successes": int(delivery["injection_successes"] or 0),
            "tool_calls": tool_calls,
            "prefetch": prefetch,
            "delivery": delivery,
        }

    @property
    def _failure_count(self) -> int:
        return self._client._failure_count

    @_failure_count.setter
    def _failure_count(self, value: int) -> None:
        self._client._failure_count = value

    @property
    def _circuit_open_until(self) -> float:
        return self._client._circuit_open_until

    @_circuit_open_until.setter
    def _circuit_open_until(self, value: float) -> None:
        self._client._circuit_open_until = value

    def is_available(self) -> bool:
        """返回提供器是否由统一配置启用。"""
        return self.enabled

    def unavailable_reason(self) -> str:
        """仅解释由 Hermes 配置显式关闭的不可用状态。"""
        if not self.enabled:
            return "hl_mem Hermes 集成未启用；在 hl_mem.toml [hermes] 设置 enabled=true 开启"
        return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """返回提供器暴露的工具定义。"""
        return [
            {
                "name": HERMES_RECALL_TOOL_NAME,
                "description": HERMES_RECALL_TOOL_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": "要查询的历史事实 / Historical facts to search for.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 5,
                            "description": "最多返回的 claim 数量 / Maximum claims to return.",
                        },
                        "intent": {
                            "type": "string",
                            "enum": list(HERMES_RECALL_INTENTS),
                            "description": "可选召回意图 / Optional recall intent.",
                        },
                    },
                    "required": ["query"],
                },
            }
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        """执行 Hermes 暴露的只读主动召回工具，并返回紧凑 JSON 文本列表。"""
        if tool_name != HERMES_RECALL_TOOL_NAME:
            raise ValueError(f"unsupported hl_mem tool: {tool_name}")
        with self._delivery_lock:
            self._recall_tool_calls += 1

        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        limit = args.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        intent = args.get("intent")
        if intent is not None and intent not in HERMES_RECALL_INTENTS:
            raise ValueError(f"intent must be one of: {', '.join(HERMES_RECALL_INTENTS)}")

        bundle = self._prefetch_cache.fetch_now(
            str(kwargs.get("session_id") or self._session_id),
            query,
            limit=limit,
            intent=intent,
            namespace="default",
            token_budget=self._effective_token_budget(None),
            injection_context=self._injection_context("active_recall"),
        )
        if bundle is None:
            return "[]"
        claims = []
        for item in bundle.items:
            if item.type != "claim":
                continue
            text = " ".join(item.text.split())
            relevance = "n/a" if item.score is None else f"{item.score:.4f}"
            claims.append(f"{item.id} | {text} | relevance={relevance}")
            if len(claims) >= limit:
                break
        return json.dumps(claims, ensure_ascii=False, separators=(",", ":"))

    def system_prompt_block(self) -> str:
        """返回注入 Hermes 系统提示词的记忆状态。"""
        notice = self._conflict_notice.render(
            self._client, self._can_call, self._on_success, self._on_failure, self._session_id
        )
        consecutive_failures = int(self._prefetch_cache.health()["consecutive_failures"] or 0)
        if self._conflict_notice.failed or self.state != "closed" or consecutive_failures >= 3:
            return (
                "# hl_mem Memory\n"
                "Status: Degraded.\n"
                "The passive memory injection and hl_mem_recall may be unavailable; continue without assuming memory "
                "context.\n"
                "Do not treat a missing result as proof that no history exists.\n"
                "Retry only after the memory service recovers."
            )
        healthy = (
            "# hl_mem Memory\n"
            "Status: healthy — passive memory injection and the read-only hl_mem_recall tool are available.\n"
            "Use hl_mem_recall before deployment/upgrade/operations, when environment surprises appear, or when prior "
            "decisions matter.\n"
            "Skip it when the memories already injected into this conversation are sufficient."
        )
        return healthy.replace("\n", f"\n{notice}\n", 1) if notice else healthy

    def initialize(self, session_id: str | None = None, **kwargs: Any) -> None:
        """初始化健康状态，或保存 Hermes 提供的会话上下文。"""
        if session_id is not None:
            self._session_id = session_id
            self._hermes_home = str(kwargs.get("hermes_home") or self._hermes_home)
            return
        if not self._can_call():
            return
        try:
            self._client.get("/healthz")
            self._on_success()
        except Exception:
            logger.warning("Hermes health check failed; provider remains degraded", exc_info=True)
            self._on_failure()

    def prefetch(
        self,
        query: str,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        *,
        session_id: str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int | None = None,
        turn_id: int | str | None = None,
        projection_version: str | None = None,
    ) -> str:
        """物化并向 Hermes 返回当前 key 的预取文本。"""
        return self._deliver_prefetched(
            query,
            session_id=session_id or self._session_id,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=self._effective_token_budget(token_budget),
            turn_id=turn_id,
            projection_version=projection_version,
        )

    def sync_turn(
        self,
        content: list[dict[str, Any]] | str,
        assistant_content: str | None = None,
        *,
        session_id: str = "",
        namespace: str = "default",
        **kwargs: Any,
    ) -> None:
        """通过 Hermes 同步 hook 写入一轮对话。"""
        if isinstance(content, list):
            raise TypeError("sync_turn expects user content as str")
        namespace = _trusted_namespace(namespace)
        active_session = session_id or self._session_id
        previous_session = self._session_id
        self._session_id = active_session
        try:
            turn_id = str(kwargs.get("turn_id") or uuid.uuid4().hex)
            events = [
                self._hermes_event_payload(
                    "user",
                    content,
                    namespace=namespace,
                    metadata={"turn_id": turn_id},
                    idempotency_key=f"hermes-turn:{active_session}:{turn_id}:user",
                ),
                self._hermes_event_payload(
                    "assistant",
                    assistant_content or "",
                    namespace=namespace,
                    metadata={"turn_id": turn_id},
                    idempotency_key=f"hermes-turn:{active_session}:{turn_id}:assistant",
                ),
            ]
            if self._sync_post("/v1/events/batch", {"events": events}):
                self._prefetch_cache.invalidate_session(active_session)
        finally:
            self._session_id = previous_session or active_session
        if kwargs.get("messages"):
            self._sync_episode_sync(
                kwargs["messages"],
                content,
                active_session,
                namespace,
            )
        self.flush_delivery_receipts(
            session_id=active_session,
            max_items=8,
        )
        return None

    def _sync_episode_sync(
        self,
        messages: list[dict[str, Any]],
        goal_content: str,
        session_id: str,
        namespace: str,
    ) -> None:
        async def sync() -> None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await self._sync_episode(
                    client,
                    messages,
                    goal_content=goal_content,
                    session_id=session_id or None,
                    namespace=namespace,
                )

        try:
            import asyncio

            asyncio.run(sync())
        except (RuntimeError, httpx.HTTPError):
            logger.warning(
                "Hermes episode synchronization failed; turn sync continues",
                exc_info=True,
            )
            return

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        *,
        namespace: str = "default",
    ) -> None:
        namespace = _trusted_namespace(namespace)
        payload: dict[str, Any] = {
            "text": content,
            "qualifiers": {"action": action, "target": target},
            "idempotency_key": _memory_idempotency_key(
                action,
                target,
                content,
                namespace,
            ),
            "namespace": namespace,
        }
        if self._session_id:
            payload["session_id"] = self._session_id
        written = self._sync_post(
            "/v1/memories",
            payload,
        )
        if written:
            self._prefetch_cache.invalidate_session(self._session_id)

    def on_pre_compress(
        self,
        messages: list[dict[str, Any]],
        *,
        namespace: str = "default",
    ) -> None:
        namespace = _trusted_namespace(namespace)
        if not self._can_call():
            return
        for message in messages:
            if not self._sync_post(
                "/v1/events",
                self._event_payload(message, namespace=namespace, session_id=self._session_id or None),
            ):
                break

    def shutdown(self) -> None:
        self._prefetch_cache.shutdown(self.timeout)
        self.flush_delivery_receipts()

    def queue_prefetch(
        self,
        query: str,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        *,
        session_id: str = "",
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int | None = None,
        projection_version: str | None = None,
    ) -> None:
        """排队 receipt-free retrieval，并完整传递所有结果影响参数。"""
        self._prefetch_cache.queue(
            query,
            session_id or self._session_id,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=self._effective_token_budget(token_budget),
            projection_version=projection_version,
            injection_context=self._injection_context("passive_injection"),
        )

    def prefetched(
        self,
        query: str = "",
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        *,
        session_id: str = "",
        known_as_of: str | None = None,
        namespace: str = "default",
        token_budget: int | None = None,
        turn_id: int | str | None = None,
        projection_version: str | None = None,
    ) -> str:
        """物化并交付 Hermes 会话已经缓存的结构化 bundle。"""
        return self._deliver_prefetched(
            query,
            session_id=session_id or self._session_id,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=self._effective_token_budget(token_budget),
            turn_id=turn_id,
            projection_version=projection_version,
        )

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        namespace: str = "default",
        **kwargs: Any,
    ) -> None:
        """记录 Hermes 委派任务及其子代理结果。"""
        del kwargs
        namespace = _trusted_namespace(namespace)
        qualifiers = {"child_session_id": child_session_id} if child_session_id else None
        self._sync_post(
            "/v1/events",
            self._hermes_event_payload(
                "user",
                task,
                qualifiers,
                namespace=namespace,
            ),
        )
        self._sync_post(
            "/v1/events",
            self._hermes_event_payload(
                "assistant",
                result,
                qualifiers,
                namespace=namespace,
            ),
        )

    def on_session_end(
        self,
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        """处理 Hermes 会话结束钩子。"""
        session_id = str(kwargs.get("session_id") or self._session_id)
        self.flush_delivery_receipts(session_id=session_id)
        self._prefetch_cache.invalidate_session(session_id)
        with self._delivery_lock:
            self._session_turns.pop(session_id, None)

    def flush_delivery_receipts(
        self,
        *,
        session_id: str | None = None,
        max_items: int | None = None,
    ) -> int:
        """有限重试尚未确认的 injected 标记，并返回本次成功数。"""
        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be non-negative")
        with self._delivery_lock:
            pending_count = sum(
                session_id is None or pending.receipt.session == session_id for pending in self._pending_injections
            )
        attempt_budget = pending_count if max_items is None else min(pending_count, max_items)
        succeeded = 0
        for _ in range(attempt_budget):
            with self._delivery_lock:
                pending = self._pop_pending_injection_locked(session_id)
                if pending is None:
                    break
            outcome = self._try_mark_injected(pending.receipt)
            if outcome is not None and pending.attempts:
                with self._delivery_lock:
                    self._delivery_health["injection_retries"] = (
                        int(self._delivery_health["injection_retries"] or 0) + 1
                    )
            if outcome is True:
                succeeded += 1
                continue
            if outcome is None:
                with self._delivery_lock:
                    queue_full = self._enqueue_pending_injection_locked(pending)
                if queue_full:
                    logger.error("Hermes injected retry queue full; oldest receipt abandoned")
                continue
            attempts = pending.attempts + 1
            queue_full = False
            with self._delivery_lock:
                if attempts >= MAX_INJECTION_ATTEMPTS:
                    self._delivery_health["injection_abandoned"] = (
                        int(self._delivery_health["injection_abandoned"] or 0) + 1
                    )
                else:
                    queue_full = self._enqueue_pending_injection_locked(_PendingInjection(pending.receipt, attempts))
            if queue_full:
                logger.error("Hermes injected retry queue full; oldest receipt abandoned")
        return succeeded

    def _pop_pending_injection_locked(
        self,
        session_id: str | None,
    ) -> _PendingInjection | None:
        for _ in range(len(self._pending_injections)):
            pending = self._pending_injections.popleft()
            if session_id is None or pending.receipt.session == session_id:
                return pending
            self._pending_injections.append(pending)
        return None

    def _deliver_prefetched(
        self,
        query: str,
        *,
        session_id: str,
        limit: int,
        intent: str | None,
        as_of: str | None,
        known_as_of: str | None,
        namespace: str,
        token_budget: int,
        turn_id: int | str | None,
        projection_version: str | None,
    ) -> str:
        injection_context = self._injection_context("passive_injection")
        bundle = self._prefetch_cache.get(
            session_id,
            query,
            limit=limit,
            intent=intent,
            as_of=as_of,
            known_as_of=known_as_of,
            namespace=namespace,
            token_budget=token_budget,
            projection_version=projection_version,
            injection_context=injection_context,
        )
        if bundle is None:
            with self._delivery_lock:
                self._delivery_health["bundle_misses"] = int(self._delivery_health["bundle_misses"] or 0) + 1
            bundle = self._prefetch_cache.fetch_now(
                session_id,
                query,
                limit=limit,
                intent=intent,
                as_of=as_of,
                known_as_of=known_as_of,
                namespace=namespace,
                token_budget=token_budget,
                projection_version=projection_version,
                injection_context=injection_context,
            )
            if bundle is None:
                return ""

        try:
            packet = self._materialize_prefetched_bundle(bundle)
        except UnknownSchemaMajorError as error:
            self._record_delivery_failure("schema_failures", error)
            logger.warning(
                "Hermes packet schema is unsupported; returning empty context",
                exc_info=True,
            )
            return ""

        render_payload = packet if packet is not None else retrieval_bundle_to_dict(bundle)
        try:
            rendered = render_context(render_payload)
        except ValueError as error:
            self._record_delivery_failure("schema_failures", error)
            logger.warning(
                "Hermes renderer rejected packet schema; returning empty context",
                exc_info=True,
            )
            return ""
        if not rendered.text:
            return ""

        feedback_ids = (
            rendered.included_feedback_ids if packet is not None and packet.get("feedback_state") == "available" else ()
        )
        self._record_delivery(
            session_id=session_id,
            turn_id=turn_id,
            query_id=bundle.query_id,
            feedback_ids=feedback_ids,
        )

        # The string return is the Hermes host/model input boundary. Durable
        # receipts are only queued here; a later lifecycle flush marks injected,
        # so daemon latency cannot delay or cancel the Agent delivery.
        return rendered.text

    def _injection_context(self, delivery_purpose: DeliveryPurpose) -> InjectionContext:
        """Freeze policy variants and the rendering clock for one cache operation."""
        return InjectionContext.create(
            delivery_purpose=delivery_purpose,
            experiment_variant="control",
            echo_variant=str(getattr(self.settings, "echo_suppression_mode", "off")),
            freshness_variant=str(getattr(self.settings, "freshness_annotation_mode", "off")),
            rendering_now=datetime.now(timezone.utc).isoformat(),
        )

    def _materialize_prefetched_bundle(
        self,
        bundle: RetrievalBundle,
    ) -> dict[str, Any] | None:
        try:
            if not self._client.can_call():
                raise RuntimeError("memory daemon circuit is open")
            response = self._client.materialize_context_packet(retrieval_bundle_to_dict(bundle))
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("materialization response must be an object")
            packet = payload.get("context_packet", payload)
            if not isinstance(packet, dict):
                raise TypeError("materialization response is missing context_packet")
            schema_major = packet.get("schema_major")
            if (
                not isinstance(schema_major, int)
                or isinstance(schema_major, bool)
                or schema_major != CONTEXT_PACKET_SCHEMA_MAJOR
            ):
                raise UnknownSchemaMajorError(f"unsupported context packet schema major: " f"{schema_major!r}")
            if packet.get("query_id") != bundle.query_id:
                raise TypeError("materialized packet query_id does not match cached bundle")
            if packet.get("feedback_state") not in {
                "available",
                "degraded",
            }:
                raise TypeError("materialized packet feedback_state is invalid")
            packet_items = packet.get("items")
            if (
                not isinstance(packet_items, list)
                or len(packet_items) != len(bundle.items)
                or any(
                    not isinstance(packet_item, dict)
                    or (
                        packet_item.get("type"),
                        packet_item.get("id"),
                        packet_item.get("text"),
                    )
                    != (
                        bundle_item.type,
                        bundle_item.id,
                        bundle_item.text,
                    )
                    or not isinstance(
                        packet_item.get("feedback_id"),
                        str,
                    )
                    or not packet_item["feedback_id"].strip()
                    for packet_item, bundle_item in zip(
                        packet_items,
                        bundle.items,
                    )
                )
            ):
                raise TypeError("materialized packet items do not match cached bundle")
            self._client.on_success()
            return packet
        except UnknownSchemaMajorError:
            self._client.on_failure()
            raise
        except Exception as error:
            self._client.on_failure()
            self._record_delivery_failure(
                "materialization_failures",
                error,
            )
            logger.warning(
                "Hermes packet materialization failed; rendering cached bundle",
                exc_info=True,
            )
            return None

    def _try_mark_injected(
        self,
        receipt: DeliveryReceipt,
    ) -> bool | None:
        if not self._client.can_call():
            with self._delivery_lock:
                self._delivery_health["injection_deferred"] = int(self._delivery_health["injection_deferred"] or 0) + 1
            return None
        try:
            self._client.mark_feedback_injected(list(receipt.feedback_ids))
            self._client.on_success()
            with self._delivery_lock:
                self._delivery_health["injection_successes"] = (
                    int(self._delivery_health["injection_successes"] or 0) + 1
                )
            return True
        except Exception as error:
            self._client.on_failure()
            self._record_delivery_failure("injection_failures", error)
            logger.warning(
                "Hermes injected marking failed; delivery remains available",
                exc_info=True,
            )
            return False

    def _record_delivery(
        self,
        *,
        session_id: str,
        turn_id: int | str | None,
        query_id: str,
        feedback_ids: tuple[str, ...],
    ) -> None:
        queue_full = False
        with self._delivery_lock:
            if turn_id is None:
                next_turn = self._session_turns.get(session_id, 0) + 1
                self._session_turns[session_id] = next_turn
                turn: int | str = next_turn
            else:
                turn = turn_id
            receipt = DeliveryReceipt(
                session=session_id,
                turn=turn,
                query_id=query_id,
                feedback_ids=feedback_ids,
            )
            self._delivery_receipts.append(receipt)
            self._delivery_health["deliveries"] = int(self._delivery_health["deliveries"] or 0) + 1
            if receipt.feedback_ids:
                queue_full = self._enqueue_pending_injection_locked(_PendingInjection(receipt, 0))
        if queue_full:
            logger.error("Hermes injected retry queue full; oldest receipt abandoned")

    def _enqueue_pending_injection_locked(
        self,
        pending: _PendingInjection,
    ) -> bool:
        queue_full = len(self._pending_injections) >= MAX_DELIVERY_RECEIPTS
        if queue_full:
            self._pending_injections.popleft()
            self._delivery_health["injection_abandoned"] = int(self._delivery_health["injection_abandoned"] or 0) + 1
            self._delivery_health["last_error"] = "RetryQueueFull"
        self._pending_injections.append(pending)
        return queue_full

    def _record_delivery_failure(
        self,
        metric: str,
        error: Exception,
    ) -> None:
        with self._delivery_lock:
            self._delivery_health[metric] = int(self._delivery_health[metric] or 0) + 1
            self._delivery_health["last_error"] = type(error).__name__

    def _effective_token_budget(self, token_budget: int | None) -> int:
        return self.settings.packed_context_token_budget if token_budget is None else token_budget

    def _sync_post(self, path: str, payload: dict[str, Any]) -> bool:
        if not self._can_call():
            return False
        try:
            self._client.post(path, payload)
            self._on_success()
            return True
        except Exception:
            logger.warning("Hermes memory write failed; request degraded", exc_info=True)
            self._on_failure()
            return False

    async def _sync_episode(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, Any]],
        *,
        goal_content: str,
        session_id: str | None,
        namespace: str = "default",
    ) -> None:
        namespace = _trusted_namespace(namespace)
        turn_messages = _messages_after_last_user(messages)
        tool_calls = self._mapper.tool_calls(turn_messages)
        if len(tool_calls) < 2:
            return
        observations = {
            str(message.get("tool_call_id", "")): str(message.get("content", ""))
            for message in turn_messages
            if message.get("role") == "tool"
        }
        goal = _episode_goal(goal_content)
        task_type = self._mapper.task_type([call["action"] for call in tool_calls])
        payload = {
            "goal": goal,
            "namespace": namespace,
            "session_id": session_id,
            "task_type": task_type,
        }
        try:
            response = await self._client.async_post(client, "/v1/episodes", payload)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 422:
                logger.warning(
                    "Hermes episode rejected status=422 goal_length=%d session_id_length=%d "
                    "task_type_length=%d response_body=%s",
                    len(goal),
                    len(session_id or ""),
                    len(task_type),
                    _validation_response_body(error.response),
                )
            raise
        episode_id = response.json()["id"]
        has_error = False
        for call in tool_calls:
            observation = observations.get(call["id"])
            error_signature = self._mapper.error_signature(observation)
            has_error = has_error or error_signature is not None
            await self._client.async_post(
                client,
                f"/v1/episodes/{episode_id}/traces",
                {
                    "action": call["action"][:MAX_TRACE_ACTION_LENGTH],
                    "observation": _summarize_observation(observation) if observation is not None else None,
                    "error_signature": error_signature,
                    "value": 0.0 if error_signature else 1.0,
                },
            )
        final_answer = any(message.get("role") == "assistant" and message.get("content") for message in turn_messages)
        status = "failed" if has_error and not final_answer else "success"
        reward = 0.2 if status == "failed" else (0.5 if has_error else 0.8)
        await self._client.async_patch(
            client,
            f"/v1/episodes/{episode_id}",
            {
                "status": status,
                "reward": reward,
                "outcome_summary": "turn completed" if final_answer else status,
            },
        )

    _tool_calls = staticmethod(EpisodeMapper.tool_calls)
    _task_type = staticmethod(EpisodeMapper.task_type)
    _error_signature = staticmethod(EpisodeMapper.error_signature)

    def _can_call(self) -> bool:
        return self._client.can_call()

    def _on_success(self) -> None:
        self._client.on_success()

    def _on_failure(self) -> None:
        self._client.on_failure()

    @staticmethod
    def _event_payload(
        message: dict[str, Any],
        *,
        namespace: str = "default",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        role = message.get("role", "user")
        payload = {
            "event_type": "message",
            "actor_type": role,
            "content": {"text": str(message.get("content", ""))},
            "namespace": _trusted_namespace(namespace),
        }
        if session_id:
            payload["session_id"] = session_id
        return payload

    def _hermes_event_payload(
        self,
        role: str,
        content: str,
        qualifiers: dict[str, Any] | None = None,
        *,
        namespace: str = "default",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_type": "message",
            "actor_type": role,
            "content": {"text": content},
            "session_id": self._session_id or None,
            "namespace": _trusted_namespace(namespace),
        }
        if qualifiers:
            payload["content"]["qualifiers"] = qualifiers
        if metadata:
            payload["metadata"] = metadata
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return payload
