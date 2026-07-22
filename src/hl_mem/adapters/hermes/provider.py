from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx


class HLMemProvider:
    """Hermes-compatible HTTP adapter with graceful degradation."""

    def __init__(self, db_path: str | None = None, daemon_url: str | None = None, timeout: float = 2.0) -> None:
        self.db_path = db_path
        self.daemon_url = (daemon_url or os.getenv("HL_MEM_URL", "http://127.0.0.1:8200")).rstrip("/")
        self.timeout = timeout
        self._failure_count = 0
        self._failure_threshold = 5
        self._circuit_open_until = 0.0
        self._last_check = 0.0
        self._health_check_interval = 30.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cache: dict[str, str] = {}
        self._session_id = ""
        self._hermes_home = ""

    @property
    def name(self) -> str:
        """返回 Hermes 使用的提供器名称。"""
        return "hl_mem"

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
            response = httpx.get(f"{self.daemon_url}/healthz", timeout=self.timeout)
            response.raise_for_status()
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
        """通过异步 HTTP 接口执行实时召回。"""
        if not self._can_call():
            return {"results": [], "error": "circuit_open"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                payload = {"query": query, "limit": limit}
                if intent is not None:
                    payload["intent"] = intent
                if as_of is not None:
                    payload["as_of"] = as_of
                response = await client.post(f"{self.daemon_url}/v1/recall", json=payload)
                response.raise_for_status()
                self._on_success()
                return response.json()
        except Exception as error:
            self._on_failure()
            return {"results": [], "error": self._error_name(error)}

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
        episode_messages = kwargs.get("messages")
        if episode_messages:
            self._sync_episode_sync(episode_messages, active_session)
        return None

    async def _sync_messages(self, messages: list[dict[str, Any]]) -> None:
        """通过异步 HTTP 接口同步结构化消息。"""
        if not self._can_call():
            return
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for message in messages:
                    payload = self._event_payload(message)
                    response = await client.post(f"{self.daemon_url}/v1/events", json=payload)
                    response.raise_for_status()
                try:
                    await self._sync_episode(client, messages)
                except Exception:
                    pass
            self._on_success()
        except Exception:
            self._on_failure()

    def _sync_episode_sync(self, messages: list[dict[str, Any]], session_id: str) -> None:
        """同步 hook 的 Episode 写入交由临时异步客户端执行。"""
        import asyncio

        async def sync() -> None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                enriched = [
                    {**message, "session_id": message.get("session_id") or session_id} for message in messages
                ]
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
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=self.timeout)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """在后台线程中预取相关记忆，供 Hermes 上下文注入使用。"""
        active_session = session_id or self._session_id

        def fetch() -> None:
            if not self._can_call():
                return
            try:
                response = httpx.post(
                    f"{self.daemon_url}/v1/recall",
                    json={"query": query, "session_id": active_session or None},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                rendered = "\n".join(
                    str(item.get("text", "")) for item in payload.get("results", []) if item.get("text")
                )
                self._on_success()
            except Exception:
                self._on_failure()
                rendered = ""
            with self._lock:
                self._cache[active_session] = rendered

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=fetch, name="hl-mem-prefetch", daemon=True)
            self._thread.start()

    def prefetched(self, *, session_id: str = "") -> str:
        """返回 Hermes 会话已经缓存的预取文本。"""
        active_session = session_id or self._session_id
        with self._lock:
            return self._cache.get(active_session, "")

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
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
            response = httpx.post(f"{self.daemon_url}{path}", json=payload, timeout=self.timeout)
            response.raise_for_status()
            self._on_success()
            return True
        except Exception:
            self._on_failure()
            return False

    async def _sync_episode(self, client: httpx.AsyncClient, messages: list[dict[str, Any]]) -> None:
        """将包含多个工具调用的 turn 旁路记录为 Episode。"""
        tool_calls = self._tool_calls(messages)
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
        task_type = self._task_type([call["action"] for call in tool_calls])
        response = await client.post(
            f"{self.daemon_url}/v1/episodes",
            json={"goal": goal, "session_id": session_id, "task_type": task_type},
        )
        response.raise_for_status()
        episode_id = response.json()["id"]
        has_error = False
        for call in tool_calls:
            observation = observations.get(call["id"])
            error_signature = self._error_signature(observation)
            has_error = has_error or error_signature is not None
            trace = await client.post(
                f"{self.daemon_url}/v1/episodes/{episode_id}/traces",
                json={
                    "action": call["action"],
                    "observation": observation,
                    "error_signature": error_signature,
                    "value": 0.0 if error_signature else 1.0,
                },
            )
            trace.raise_for_status()
        goal_index = messages.index(goal_message) if goal_message else -1
        final_answer = any(
            message.get("role") == "assistant" and message.get("content")
            for message in messages[goal_index + 1 :]
        )
        status = "failed" if has_error and not final_answer else "success"
        reward = 0.2 if status == "failed" else (0.5 if has_error else 0.8)
        outcome = await client.patch(
            f"{self.daemon_url}/v1/episodes/{episode_id}",
            json={"status": status, "reward": reward, "outcome_summary": "turn completed" if final_answer else status},
        )
        outcome.raise_for_status()

    @staticmethod
    def _tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        structured: list[dict[str, str]] = []
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                structured.append({"id": str(call.get("id", "")), "action": str(function.get("name") or "tool")})
        if structured:
            return structured
        return [
            {
                "id": str(message.get("tool_call_id", index)),
                "action": str(message.get("name") or "tool"),
            }
            for index, message in enumerate(messages)
            if message.get("role") == "tool"
        ]

    @staticmethod
    def _task_type(actions: list[str]) -> str:
        lowered = [action.lower() for action in actions]
        if any(any(marker in action for marker in ("terminal", "read_file", "patch")) for action in lowered):
            return "coding"
        if any("web_search" in action for action in lowered):
            return "research"
        return "general"

    @staticmethod
    def _error_signature(observation: str | None) -> str | None:
        if observation and any(marker in observation.lower() for marker in ("error", "failed", "exception")):
            return observation[:500]
        return None

    def _can_call(self) -> bool:
        return time.monotonic() >= self._circuit_open_until

    def _on_success(self) -> None:
        self._failure_count = 0

    def _on_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._circuit_open_until = time.monotonic() + 60.0
            self._failure_count = 0

    @staticmethod
    def _event_payload(message: dict[str, Any]) -> dict[str, Any]:
        role = message.get("role", "user")
        return {"event_type": "message", "actor_type": role, "content": {"text": str(message.get("content", ""))}}

    def _hermes_event_payload(
        self,
        role: str,
        content: str,
        qualifiers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建包含 Hermes 会话信息的事件请求体。"""
        payload: dict[str, Any] = {
            "event_type": "message",
            "actor_type": role,
            "content": {"text": content},
            "session_id": self._session_id or None,
        }
        if qualifiers:
            payload["content"]["qualifiers"] = qualifiers
        return payload

    @staticmethod
    def _error_name(error: Exception) -> str:
        return "timeout" if isinstance(error, httpx.TimeoutException) else "unavailable"


HermesMemoryProvider = HLMemProvider
