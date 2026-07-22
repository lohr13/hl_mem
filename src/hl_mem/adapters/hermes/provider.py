from __future__ import annotations

import time
from typing import Any

import httpx


class HLMemProvider:
    """Hermes-compatible HTTP adapter with graceful degradation."""

    def __init__(self, db_path: str | None = None, daemon_url: str | None = None, timeout: float = 2.0) -> None:
        self.db_path = db_path
        self.daemon_url = (daemon_url or "http://127.0.0.1:8000").rstrip("/")
        self.timeout = timeout
        self._failure_count = 0
        self._failure_threshold = 5
        self._circuit_open_until = 0.0
        self._last_check = 0.0
        self._health_check_interval = 30.0

    def initialize(self) -> None:
        if not self._can_call():
            return
        try:
            response = httpx.get(f"{self.daemon_url}/healthz", timeout=self.timeout)
            response.raise_for_status()
            self._on_success()
        except Exception:
            self._on_failure()

    async def prefetch(
        self, query: str, limit: int = 10, intent: str | None = None, as_of: str | None = None
    ) -> dict[str, Any]:
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

    async def sync_turn(self, messages: list[dict[str, Any]]) -> None:
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

    def on_memory_write(self, key: str, content: str, target: str = "memory") -> None:
        self._sync_post("/v1/memories", {"text": content, "qualifiers": {"key": key, "target": target}})

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> None:
        if not self._can_call():
            return
        for message in messages:
            if not self._sync_post("/v1/events", self._event_payload(message)):
                break

    def shutdown(self) -> None:
        return None

    def _sync_post(self, path: str, payload: dict[str, Any]) -> bool:
        if not self._can_call():
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
        try:
            response = httpx.post(f"{self.daemon_url}{path}", json=payload, timeout=self.timeout)
            response.raise_for_status()
            self._on_success()
            return True
        except Exception:
            self._on_failure()
            return False

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

    @staticmethod
    def _error_name(error: Exception) -> str:
        return "timeout" if isinstance(error, httpx.TimeoutException) else "unavailable"
