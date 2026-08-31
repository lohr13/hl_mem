"""无传输耦合的 MCP 工具契约实现。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import components
from hl_mem.application.correction import CorrectionService
from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService
from hl_mem.application.memories import MemoryQueryService
from hl_mem.application.recall import RecallService
from hl_mem.application.recall_side_effects import DeferredLLMSpanRecorder, RecallSideEffectDispatcher
from hl_mem.compatibility import CONTEXT_PACKET_SCHEMA_MAJOR, CONTEXT_PACKET_SCHEMA_MINOR
from hl_mem.domain.temporal import RecallIntent, parse_utc
from hl_mem.errors import NotFoundError, ValidationError
from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository


def _context_packet_schema() -> dict[str, Any]:
    """返回与 REST DTO 一致的严格 Context Packet v1 JSON Schema。"""
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {
                "type": "string",
                "enum": ["claim", "observation", "policy", "episode", "trace"],
            },
            "id": {"type": "string"},
            "text": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "feedback_id": {
                "type": "string",
                "minLength": 1,
                "pattern": r".*\S.*",
            },
            "role": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
            "action": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
            "object": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
        },
        "required": ["type", "id", "text", "evidence", "feedback_id"],
        "allOf": [
            {
                "if": {"anyOf": [{"required": ["role"]}, {"required": ["action"]}, {"required": ["object"]}]},
                "then": {
                    "properties": {"type": {"const": "claim"}},
                    "required": ["role", "action", "object"],
                },
            }
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_major": {"type": "integer", "const": CONTEXT_PACKET_SCHEMA_MAJOR},
            "schema_minor": {"type": "integer", "const": CONTEXT_PACKET_SCHEMA_MINOR},
            "query_id": {"type": "string"},
            "answerability": {
                "type": "string",
                "enum": ["supported", "low_confidence", "no_evidence"],
            },
            "feedback_state": {
                "type": "string",
                "enum": ["available", "degraded"],
            },
            "items": {"type": "array", "items": item_schema},
            "used_tokens_estimate": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
        },
        "required": [
            "schema_major",
            "schema_minor",
            "query_id",
            "answerability",
            "feedback_state",
            "items",
            "used_tokens_estimate",
            "truncated",
        ],
    }


def get_tool_schemas() -> list[dict[str, Any]]:
    """返回稳定、可快照化的 MCP 工具 JSON Schema。"""
    object_schema = {"type": "object", "additionalProperties": True}
    return [
        {
            "name": "memory_recall",
            "description": "Recall relevant memories.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "as_of": {"type": "string"},
                    "known_as_of": {"type": "string"},
                    "intent": {"type": "string"},
                    "token_budget": {"type": "integer", "minimum": 1},
                    "context_mode": {"type": "string", "enum": ["packed"]},
                    "response_format": {
                        "type": "string",
                        "enum": ["legacy", "context_packet", "both"],
                        "default": "legacy",
                    },
                    "namespace": {"type": "string"},
                    "session_id": {"type": "string"},
                    "debug": {"type": "boolean"},
                },
                "required": ["query"],
            },
            "outputSchema": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "context_packet": _context_packet_schema(),
                    "results": {"type": "array"},
                    "observations": {"type": "array"},
                    "policies": {"type": "array"},
                    "total": {"type": "integer"},
                    "query_id": {"type": "string"},
                    "answerability": {
                        "type": "string",
                        "enum": ["supported", "low_confidence", "no_evidence"],
                    },
                    "context": {"type": "object"},
                    "search_trace": {"type": "object"},
                },
            },
        },
        {
            "name": "memory_save",
            "description": "Save an explicit memory.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "text": {"type": "string"},
                    "content": {"type": "string"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "qualifiers": {"type": "object"},
                    "idempotency_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "namespace": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
                },
            },
        },
        {
            "name": "memory_forget",
            "description": "Forget a memory by identifier.",
            "inputSchema": {
                **object_schema,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_get",
            "description": "Get complete claim details by identifier.",
            "inputSchema": {
                **object_schema,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_correct",
            "description": "Replace only a claim's content and preserve its classification.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "id": {"type": "string"},
                    "corrected_text": {"type": "string", "minLength": 1, "maxLength": 50000},
                    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "required": ["id", "corrected_text"],
            },
        },
        {
            "name": "memory_explain",
            "description": "Explain a memory's evidence chain.",
            "inputSchema": {
                **object_schema,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_feedback",
            "description": "Submit usefulness feedback.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "feedback_id": {"type": "string"},
                    "helpful": {"type": "boolean"},
                    "task_outcome": {"type": "number", "minimum": 0, "maximum": 1},
                    "correction": {"type": "object"},
                },
                "required": ["feedback_id", "helpful"],
            },
        },
    ]


class McpMemoryServer:
    """提供可嵌入任意 MCP 传输层的最小记忆工具集。"""

    _TOOLS = tuple(tool["name"] for tool in get_tool_schemas())

    def __init__(
        self,
        settings: Settings | str | Path,
        embedder: Any = None,
        reranker: Any = None,
        provider_runtime: Any = None,
    ) -> None:
        """使用已加载的统一配置创建 MCP 服务。"""
        if not isinstance(settings, Settings):
            settings = replace(Settings(), database_path=str(settings))
        components.initialize_process(settings)
        self.database = Database(settings=settings)
        self.database.open_worker()
        self.settings = settings
        self.provider_runtime = provider_runtime
        self._owns_provider_runtime = False
        if self.provider_runtime is None and (settings.llm_api_key or settings.query_expansion_api_key):
            self.provider_runtime = components.create_provider_runtime(settings)
            self._owns_provider_runtime = True
        self.embedder = embedder or components.make_embedder(settings)
        self.reranker = reranker if reranker is not None else components.make_reranker(settings)
        self.recall_side_effects = RecallSideEffectDispatcher(self.database, settings=settings)
        self.deferred_llm_spans = DeferredLLMSpanRecorder(self.recall_side_effects)

    def list_tools(self) -> tuple[str, ...]:
        """返回稳定的 MCP 工具名称。"""
        return self._TOOLS

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用一个记忆工具并返回 JSON 可序列化结果。"""
        if name not in self._TOOLS:
            raise ValidationError(f"unknown MCP tool: {name}")
        if name == "memory_recall":
            with self.database.connect_readonly() as connection:
                return self._recall(connection, arguments)
        with self.database.connect() as connection:
            if name == "memory_save":
                return self._save(connection, arguments)
            if name == "memory_forget":
                return self._forget(connection, arguments)
            if name == "memory_get":
                return self._get(connection, arguments)
            if name == "memory_correct":
                return self._correct(connection, arguments)
            if name == "memory_feedback":
                return self._feedback(connection, arguments)
            return self._explain(connection, arguments)

    def _save(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """原子保存显式记忆事件并创建提取任务。"""
        text = arguments.get("text") or arguments.get("content")
        if not isinstance(text, str) or not text:
            raise ValidationError("text or content is required")
        idempotency_key = arguments.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise ValidationError("idempotency_key must be a string")
            if not idempotency_key:
                raise ValidationError("idempotency_key must not be empty")
            if len(idempotency_key) > 200:
                raise ValidationError("idempotency_key must be at most 200 characters")
        namespace = arguments.get("namespace", "default")
        if not isinstance(namespace, str) or not namespace:
            raise ValidationError("namespace must be a non-empty string")
        if len(namespace) > 100:
            raise ValidationError("namespace must be at most 100 characters")
        qualifiers = arguments.get("qualifiers") or {}
        if not isinstance(qualifiers, dict):
            raise ValidationError("qualifiers must be an object")
        result = IngestService(connection).save_explicit_memory(
            text,
            str(arguments.get("subject", "用户")),
            str(arguments.get("predicate", "explicit_memory")),
            qualifiers,
            idempotency_key=idempotency_key,
            namespace=namespace,
        )
        return result

    def _recall(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过共享召回服务执行混合召回。"""
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            raise ValidationError("query is required")
        limit = arguments.get("limit", self.settings.recall_default_limit)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        token_budget = arguments.get("token_budget")
        if token_budget is not None and (
            isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 1
        ):
            raise ValidationError("token_budget must be a positive integer")
        for field_name in ("as_of", "known_as_of"):
            timestamp = arguments.get(field_name)
            if timestamp is not None:
                if not isinstance(timestamp, str):
                    raise ValidationError(f"{field_name} must be an ISO-8601 string")
                try:
                    parse_utc(timestamp)
                except ValueError as error:
                    raise ValidationError(str(error)) from error
        intent = arguments.get("intent")
        if intent is not None:
            try:
                RecallIntent(intent)
            except (TypeError, ValueError) as error:
                raise ValidationError(f"unsupported recall intent: {intent}") from error
        response_format = arguments.get("response_format", "legacy")
        if response_format not in {"legacy", "context_packet", "both"}:
            raise ValidationError(f"unsupported response_format: {response_format}")
        return RecallService(
            connection,
            self.embedder,
            self.reranker,
            settings=self.settings,
            query_expander=components.make_query_expander(
                self.settings,
                span_recorder=self.deferred_llm_spans,
                runtime=self.provider_runtime,
            ),
            side_effect_sink=self.recall_side_effects,
        ).recall(
            query=query,
            limit=limit,
            as_of=arguments.get("as_of"),
            intent=intent,
            known_as_of=arguments.get("known_as_of"),
            query_id=arguments.get("query_id"),
            token_budget=token_budget,
            context_mode=arguments.get("context_mode"),
            response_format=response_format,
            namespace=str(arguments.get("namespace", "default")),
            session_id=arguments.get("session_id"),
            debug=bool(arguments.get("debug", False)),
        )

    def close(self) -> None:
        """排空 recall 副作用并关闭数据库资源。"""
        closed = self.recall_side_effects.close(self.recall_side_effects.recommended_shutdown_timeout)
        if self._owns_provider_runtime and self.provider_runtime is not None:
            self.provider_runtime.close()
            self.provider_runtime = None
        if closed:
            self.database.close()

    @staticmethod
    def _forget(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过统一删除闭包写入墓碑并物理删除 claim。"""
        memory_id = str(arguments.get("id", ""))
        try:
            return ForgetService(connection).forget(memory_id)
        except ValueError as error:
            if not str(error).startswith("memory not found"):
                raise
            return {"id": memory_id, "forgotten": False}

    def _feedback(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 feedback_id 提交 usefulness，并仅在显式 correction 字段存在时纠正记忆。"""
        feedback_id = arguments.get("feedback_id")
        if not isinstance(feedback_id, str) or not feedback_id:
            raise ValidationError("feedback_id is required")
        if not isinstance(arguments.get("helpful"), bool):
            raise ValidationError("helpful must be a boolean")
        task_outcome = arguments.get("task_outcome")
        if task_outcome is not None and (
            isinstance(task_outcome, bool)
            or not isinstance(task_outcome, (int, float))
            or not 0.0 <= task_outcome <= 1.0
        ):
            raise ValidationError("task_outcome must be between 0 and 1")
        now = datetime.now(timezone.utc).isoformat()
        try:
            result: dict[str, Any] = ExperienceService(
                connection,
                settings=self.settings,
                pending_exposure_check=self.recall_side_effects.has_pending_exposures,
            ).submit_retrieval_feedback_eventually(
                feedback_id,
                arguments["helpful"],
                float(task_outcome) if task_outcome is not None else None,
                now,
            )
        except ValueError as error:
            if not str(error).startswith("feedback exposure not found:"):
                raise
            raise ValidationError(str(error)) from error
        correction = arguments.get("correction")
        if not correction:
            return result
        if not isinstance(correction, dict):
            raise ValidationError("correction must be an object")
        memory_id = correction.get("memory_id")
        action = correction.get("action")
        key = correction.get("idempotency_key")
        if not isinstance(memory_id, str) or not memory_id or action not in {"retract", "replace"}:
            raise ValidationError("correction requires memory_id and retract|replace action")
        correction_result = CorrectionService(connection, self.embedder, settings=self.settings).apply(
            memory_id,
            action=action,
            corrected_text=correction.get("corrected_text"),
            idempotency_key=key,
        )
        result["correction"] = correction_result
        result["correction_event_id"] = correction_result["correction_event_id"]
        return result

    @staticmethod
    def _get(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """复用共享查询服务返回完整 Claim 详情。"""
        return MemoryQueryService(connection).get_memory(str(arguments.get("id", "")))

    def _correct(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """复用共享纠正服务执行仅内容替换。"""
        return CorrectionService(connection, self.embedder, settings=self.settings).correct(
            arguments.get("id"),
            arguments.get("corrected_text"),
            arguments.get("idempotency_key"),
        )

    @staticmethod
    def _explain(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过 repository 返回事件或 claim 的证据链。"""
        memory_id = str(arguments.get("id", ""))
        event = EventRepository(connection).get_event(memory_id)
        if event:
            return {
                "type": "event",
                "id": memory_id,
                "evidence": [{"type": "event", "id": memory_id}],
            }
        claim = ClaimRepository(connection).get_claim(memory_id)
        if not claim:
            raise NotFoundError(f"memory not found: {memory_id}")
        links = EvidenceRepository(connection).get_links_for_derived("claim", memory_id)
        evidence = [
            {
                "evidence_type": link["evidence_type"],
                "evidence_id": link["evidence_id"],
                "relation": link["relation"],
            }
            for link in links
        ]
        return {"type": "claim", "id": memory_id, "evidence": evidence}
