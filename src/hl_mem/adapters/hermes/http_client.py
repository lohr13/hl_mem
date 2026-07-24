"""Hermes 适配器使用的 HTTP 客户端与熔断器。"""

from __future__ import annotations

import time
import threading
from typing import Any

import httpx

from hl_mem.http_utils import retry_http


class HLMemHttpClient:
    """封装同步/异步 HTTP 调用、错误降级和熔断状态。"""

    def __init__(
        self,
        daemon_url: str,
        timeout: float,
        failure_threshold: int,
        circuit_open_seconds: float,
    ) -> None:
        self.daemon_url = daemon_url.rstrip("/")
        self.timeout = timeout
        self._failure_count = 0
        self._failure_threshold = failure_threshold
        self._circuit_open_until = 0.0
        self._circuit_open_seconds = circuit_open_seconds
        self._lock = threading.Lock()
        self._probe_owner: int | None = None

    @property
    def state(self) -> str:
        """返回只读熔断状态：open、closed 或 half_open。"""
        with self._lock:
            return self._state_locked()

    def can_call(self) -> bool:
        """原子判断是否允许请求，半开时只授予一个线程探测权。"""
        with self._lock:
            state = self._state_locked()
            if state == "open":
                return False
            if state == "half_open":
                if self._probe_owner is not None:
                    return False
                self._probe_owner = threading.get_ident()
            return True

    def get(self, path: str) -> httpx.Response:
        """执行同步 GET 请求。"""
        def send_request() -> httpx.Response:
            response = httpx.get(f"{self.daemon_url}{path}", timeout=self.timeout)
            response.raise_for_status()
            return response

        return retry_http(send_request)

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        """执行同步 POST 请求。"""
        def send_request() -> httpx.Response:
            response = httpx.post(f"{self.daemon_url}{path}", json=payload, timeout=self.timeout)
            response.raise_for_status()
            return response

        return retry_http(send_request)

    async def async_post(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        """使用注入的异步客户端执行 POST 请求。"""
        response = await client.post(f"{self.daemon_url}{path}", json=payload)
        response.raise_for_status()
        return response

    async def async_patch(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        """使用注入的异步客户端执行 PATCH 请求。"""
        response = await client.patch(f"{self.daemon_url}{path}", json=payload)
        response.raise_for_status()
        return response

    def on_success(self) -> None:
        """关闭熔断器并清零连续失败计数。"""
        with self._lock:
            state = self._state_locked()
            if state == "open":
                return
            if self._probe_owner is not None and self._probe_owner != threading.get_ident():
                return
            self._failure_count = 0
            self._circuit_open_until = 0.0
            self._probe_owner = None

    def on_failure(self) -> None:
        """记录失败，并在达到阈值时打开熔断器。"""
        with self._lock:
            state = self._state_locked()
            if state == "open":
                return
            if self._probe_owner is not None:
                if self._probe_owner != threading.get_ident():
                    return
                self._open_locked()
                return
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self._open_locked()

    def _state_locked(self) -> str:
        if self._circuit_open_until <= 0:
            return "closed"
        if time.monotonic() < self._circuit_open_until:
            return "open"
        return "half_open"

    def _open_locked(self) -> None:
        self._circuit_open_until = time.monotonic() + self._circuit_open_seconds
        self._failure_count = 0
        self._probe_owner = None
