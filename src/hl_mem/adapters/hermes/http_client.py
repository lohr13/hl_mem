"""Hermes 适配器使用的 HTTP 客户端与熔断器。"""

from __future__ import annotations

import time
from typing import Any

import httpx


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
        self._half_open = False
        self._circuit_open_seconds = circuit_open_seconds

    @property
    def state(self) -> str:
        """返回只读熔断状态：open、closed 或 half_open。"""
        if self._circuit_open_until <= 0:
            return "closed"
        if time.monotonic() < self._circuit_open_until:
            return "open"
        return "half_open"

    def can_call(self) -> bool:
        """判断当前是否允许请求，并标记半开探测。"""
        if self.state == "open":
            return False
        self._half_open = self.state == "half_open"
        return True

    def get(self, path: str) -> httpx.Response:
        """执行同步 GET 请求。"""
        response = httpx.get(f"{self.daemon_url}{path}", timeout=self.timeout)
        response.raise_for_status()
        return response

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        """执行同步 POST 请求。"""
        response = httpx.post(f"{self.daemon_url}{path}", json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response

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
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._half_open = False

    def on_failure(self) -> None:
        """记录失败，并在达到阈值时打开熔断器。"""
        if self._half_open:
            self._circuit_open_until = time.monotonic() + self._circuit_open_seconds
            self._failure_count = 0
            self._half_open = False
            return
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._circuit_open_until = time.monotonic() + self._circuit_open_seconds
            self._failure_count = 0
            self._half_open = False

    @staticmethod
    def error_name(error: Exception) -> str:
        """将 HTTP 异常映射为稳定的降级错误名。"""
        return "timeout" if isinstance(error, httpx.TimeoutException) else "unavailable"
