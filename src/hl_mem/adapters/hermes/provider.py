from __future__ import annotations

import time
from typing import Any

import httpx


class HLMemProvider:
    """Hermes-compatible HTTP adapter with graceful degradation."""

    def __init__(self, db_path: str | None = None, daemon_url: str | None = None,
                 timeout: float = 2.0) -> None:
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

    async def prefetch(self, query: str, limit: int = 10) -> dict[str, Any]:
        if not self._can_call():
            return {"results": [], "error": "circuit_open"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.daemon_url}/v1/recall", json={"query": query, "limit": limit}
                )
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
            self._on_success()
        except Exception:
            self._on_failure()

    def on_memory_write(self, key: str, content: str, target: str = "memory") -> None:
        self._sync_post("/v1/memories", {"text": content, "qualifiers": {
            "key": key, "target": target}})

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
        return {"event_type": "message", "actor_type": role,
                "content": {"text": str(message.get("content", ""))}}

    @staticmethod
    def _error_name(error: Exception) -> str:
        return "timeout" if isinstance(error, httpx.TimeoutException) else "unavailable"
