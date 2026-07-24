"""保持 Hermes hook 契约稳定的 HL-Mem 协调适配器。"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from hl_mem.adapters.hermes.episode_mapper import EpisodeMapper
from hl_mem.adapters.hermes.http_client import HLMemHttpClient
from hl_mem.adapters.hermes.prefetch import PrefetchCache
from hl_mem.settings import Settings


class HLMemProvider:
    """Hermes 兼容协调层；HTTP、缓存与 Episode 映射委托给独立组件。"""

    def __init__(self, db_path: str | None = None, daemon_url: str | None = None, timeout: float = 2.0) -> None:
        settings = Settings.from_env()
        self.db_path = db_path
        self.daemon_url = (daemon_url or os.getenv("HL_MEM_URL", "http://127.0.0.1:8200")).rstrip("/")
        self.timeout = timeout
        self._client = HLMemHttpClient(
            self.daemon_url,
            timeout,
            settings.hermes_circuit_failure_threshold,
            settings.hermes_circuit_open_seconds,
        )
        self._prefetch_cache = PrefetchCache(self._client)
        self._mapper = EpisodeMapper()
        self._session_id = ""
        self._hermes_home = ""

    @property
    def name(self) -> str:
        """返回 Hermes 使用的提供器名称。"""
        return "hl_mem"

    @property
    def state(self) -> str:
        """返回只读熔断状态：open、closed 或 half_open。"""
        return self._client.state

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
        """返回提供器是否由环境变量启用。"""
        return os.getenv("HL_MEM_ENABLED", "true").lower() != "false"

    def get_tool_schemas(self) -> list[Any]:
        """返回提供器暴露的工具定义。"""
        return []

    def system_prompt_block(self) -> str:
        """返回注入 Hermes 系统提示词的记忆状态。"""
        return "# hl_mem Memory\nActive. Relevant memories injected into context."

    def initialize(self, session_id: str | None = None, **kwargs: Any) -> None:
        """初始化健康状态，或保存 Hermes 提供的会话上下文。"""
        if session_id is not None:
            self._session_id = session_id
            self._hermes_home = str(kwargs.get("hermes_home") or os.getenv("HERMES_HOME", ""))
            return
        if not self._can_call():
            return
        try:
            self._client.get("/healthz")
            self._on_success()
        except Exception:
            self._on_failure()

    def prefetch(
        self,
        query: str,
        limit: int = 10,
        intent: str | None = None,
        as_of: str | None = None,
        *,
        session_id: str | None = None,
    ) -> Any:
        """预取记忆；Hermes 会话调用读取缓存，旧异步调用返回协程。"""
        if session_id is not None:
            del query, limit, intent, as_of
            return self.prefetched(session_id=session_id)
        return self._prefetch(query, limit, intent, as_of)

    async def _prefetch(
        self, query: str, limit: int, intent: str | None, as_of: str | None
    ) -> dict[str, Any]:
        if not self._can_call():
            return {"results": [], "error": "circuit_open"}
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if intent is not None:
            payload["intent"] = intent
        if as_of is not None:
            payload["as_of"] = as_of
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await self._client.async_post(client, "/v1/recall", payload)
            self._on_success()
            return response.json()
        except Exception as error:
            self._on_failure()
            return {"results": [], "error": self._client.error_name(error)}

    def sync_turn(
        self,
        content: list[dict[str, Any]] | str,
        assistant_content: str | None = None,
        *,
        session_id: str = "",
        **kwargs: Any,
    ) -> Any:
        """同步一轮对话；兼容旧异步消息列表与 Hermes 同步 hook。"""
        if isinstance(content, list):
            return self._sync_messages(content)
        active_session = session_id or self._session_id
        previous_session = self._session_id
        self._session_id = active_session
        try:
            self._sync_post("/v1/events", self._hermes_event_payload("user", content))
            self._sync_post("/v1/events", self._hermes_event_payload("assistant", assistant_content or ""))
        finally:
            self._session_id = previous_session or active_session
        if kwargs.get("messages"):
            self._sync_episode_sync(kwargs["messages"], active_session)
        return None

    async def _sync_messages(self, messages: list[dict[str, Any]]) -> None:
        if not self._can_call():
            return
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for message in messages:
                    await self._client.async_post(client, "/v1/events", self._event_payload(message))
                try:
                    await self._sync_episode(client, messages)
                except Exception:
                    pass
            self._on_success()
        except Exception:
            self._on_failure()

    def _sync_episode_sync(self, messages: list[dict[str, Any]], session_id: str) -> None:
        async def sync() -> None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                enriched = [{**message, "session_id": message.get("session_id") or session_id} for message in messages]
                await self._sync_episode(client, enriched)

        try:
            asyncio.run(sync())
        except (RuntimeError, httpx.HTTPError):
            return

    def on_memory_write(self, key: str, content: str, target: str = "memory") -> None:
        self._sync_post("/v1/memories", {"text": content, "qualifiers": {"key": key, "target": target}})

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> None:
        if not self._can_call():
            return
        for message in messages:
            if not self._sync_post("/v1/events", self._event_payload(message)):
                break

    def shutdown(self) -> None:
        self._prefetch_cache.shutdown(self.timeout)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._prefetch_cache.queue(query, session_id or self._session_id)

    def prefetched(self, *, session_id: str = "") -> str:
        """返回 Hermes 会话已经缓存的预取文本。"""
        return self._prefetch_cache.get(session_id or self._session_id)

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any
    ) -> None:
        """记录 Hermes 委派任务及其子代理结果。"""
        del kwargs
        qualifiers = {"child_session_id": child_session_id} if child_session_id else None
        self._sync_post("/v1/events", self._hermes_event_payload("user", task, qualifiers))
        self._sync_post("/v1/events", self._hermes_event_payload("assistant", result, qualifiers))

    def on_session_end(self, **kwargs: Any) -> None:
        """处理 Hermes 会话结束钩子。"""
        del kwargs

    def _sync_post(self, path: str, payload: dict[str, Any]) -> bool:
        if not self._can_call():
            return False
        try:
            self._client.post(path, payload)
            self._on_success()
            return True
        except Exception:
            self._on_failure()
            return False

    async def _sync_episode(self, client: httpx.AsyncClient, messages: list[dict[str, Any]]) -> None:
        tool_calls = self._mapper.tool_calls(messages)
        if len(tool_calls) < 2:
            return
        observations = {
            str(message.get("tool_call_id", "")): str(message.get("content", ""))
            for message in messages
            if message.get("role") == "tool"
        }
        goal_message = next((message for message in messages if message.get("role") == "user"), {})
        goal = str(goal_message.get("content") or "Complete tool-assisted task")
        session_id = next((message.get("session_id") for message in messages if message.get("session_id")), None)
        response = await self._client.async_post(
            client,
            "/v1/episodes",
            {
                "goal": goal,
                "session_id": session_id,
                "task_type": self._mapper.task_type([call["action"] for call in tool_calls]),
            },
        )
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
                    "action": call["action"],
                    "observation": observation,
                    "error_signature": error_signature,
                    "value": 0.0 if error_signature else 1.0,
                },
            )
        goal_index = messages.index(goal_message) if goal_message else -1
        final_answer = any(
            message.get("role") == "assistant" and message.get("content") for message in messages[goal_index + 1 :]
        )
        status = "failed" if has_error and not final_answer else "success"
        reward = 0.2 if status == "failed" else (0.5 if has_error else 0.8)
        await self._client.async_patch(
            client,
            f"/v1/episodes/{episode_id}",
            {"status": status, "reward": reward, "outcome_summary": "turn completed" if final_answer else status},
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
    def _event_payload(message: dict[str, Any]) -> dict[str, Any]:
        role = message.get("role", "user")
        return {"event_type": "message", "actor_type": role, "content": {"text": str(message.get("content", ""))}}

    def _hermes_event_payload(
        self, role: str, content: str, qualifiers: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_type": "message",
            "actor_type": role,
            "content": {"text": content},
            "session_id": self._session_id or None,
        }
        if qualifiers:
            payload["content"]["qualifiers"] = qualifiers
        return payload


HermesMemoryProvider = HLMemProvider
