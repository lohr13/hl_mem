"""Hermes 后台预取缓存。"""

from __future__ import annotations

import threading

from hl_mem.adapters.hermes.http_client import HLMemHttpClient


class PrefetchCache:
    """在线程中预取召回结果，并按会话缓存渲染文本。"""

    def __init__(self, client: HLMemHttpClient) -> None:
        self.client = client
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._values: dict[str, str] = {}

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
                self.client.on_failure()
                rendered = ""
            with self._lock:
                self._values[session_id] = rendered

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=fetch, name="hl-mem-prefetch", daemon=True)
            self._thread.start()

    def get(self, session_id: str) -> str:
        """读取指定会话的预取文本。"""
        with self._lock:
            return self._values.get(session_id, "")

    def shutdown(self, timeout: float) -> None:
        """等待当前预取线程结束。"""
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
