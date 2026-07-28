"""Hermes 后台预取缓存。"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass

from hl_mem.adapters.hermes.http_client import HLMemHttpClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrefetchEntry:
    """带过期时间的预取缓存条目。"""

    value: str
    expires_at: float


class PrefetchCache:
    """在线程中预取召回结果，并按会话缓存渲染文本。"""

    def __init__(self, client: HLMemHttpClient, ttl_seconds: float = 300.0) -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._values: dict[tuple[str, str], PrefetchEntry] = {}

    def queue(self, query: str, session_id: str) -> None:
        """排队一次后台预取；已有预取执行时不重复启动。"""

        def fetch() -> None:
            if not self.client.can_call():
                return
            try:
                response = self.client.post(
                    "/v1/recall",
                    {"query": query, "session_id": session_id or None},
                )
                payload = response.json()
                rendered = "\n".join(
                    str(item.get("text", "")) for item in payload.get("results", []) if item.get("text")
                )
                self.client.on_success()
            except Exception:
                logger.warning("Hermes memory prefetch failed; using empty result", exc_info=True)
                self.client.on_failure()
                rendered = ""
            key = self._key(session_id, query)
            with self._lock:
                self._values[key] = PrefetchEntry(rendered, time.monotonic() + self.ttl_seconds)

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=fetch, name="hl-mem-prefetch", daemon=True)
            self._thread.start()

    def get(self, session_id: str, query: str) -> str:
        """读取指定会话与查询的未过期预取文本。"""
        key = self._key(session_id, query)
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return ""
            if entry.expires_at <= time.monotonic():
                del self._values[key]
                return ""
            return entry.value

    def invalidate_session(self, session_id: str) -> None:
        """清理指定会话的全部预取缓存。"""
        with self._lock:
            keys = [key for key in self._values if key[0] == session_id]
            for key in keys:
                del self._values[key]

    def shutdown(self, timeout: float) -> None:
        """等待当前预取线程结束。"""
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    @staticmethod
    def _key(session_id: str, query: str) -> tuple[str, str]:
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        return session_id, query_hash
