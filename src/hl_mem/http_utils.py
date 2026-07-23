"""统一的 HTTP 重试策略。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import httpx

T = TypeVar("T")


def retry_http(
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> T:
    """按指数退避重试超时、HTTP 429 和 5xx 响应。"""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (httpx.TimeoutException, httpx.HTTPStatusError) as error:
            is_retryable = isinstance(error, httpx.TimeoutException) or (
                error.response is not None
                and (error.response.status_code == 429 or error.response.status_code >= 500)
            )
            if not is_retryable or attempt == max_attempts:
                raise
            time.sleep(base_delay * (backoff_factor ** (attempt - 1)))

    raise RuntimeError("unreachable")
