"""统一的 HTTP 重试策略。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx

T = TypeVar("T")
RetryCallback = Callable[[int, int, float, BaseException], None]


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield an exception and its explicit/implicit causes without cycles."""
    visited: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in visited:
        yield current
        visited.add(id(current))
        current = current.__cause__ or current.__context__


def find_http_status_error(error: BaseException) -> httpx.HTTPStatusError | None:
    """Find the first HTTP status failure in an exception chain."""
    return next((item for item in exception_chain(error) if isinstance(item, httpx.HTTPStatusError)), None)


def find_http_exception(
    error: BaseException,
    exception_types: tuple[type[BaseException], ...],
) -> BaseException | None:
    """Find the first selected transport exception in an exception chain."""
    return next((item for item in exception_chain(error) if isinstance(item, exception_types)), None)


def retry_after_seconds(value: str, *, now: datetime | None = None) -> float | None:
    """Parse Retry-After delta-seconds or HTTP-date into a non-negative delay."""
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (retry_at - reference).total_seconds())


def retry_http(
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
    *,
    retry_after: bool = False,
    retry_timeout_types: tuple[type[BaseException], ...] = (httpx.TimeoutException,),
    retry_connect_errors: bool = True,
    sleep: Callable[[float], None] | None = None,
    on_retry: RetryCallback | None = None,
) -> T:
    """按指数退避重试选定传输错误、HTTP 429 和 5xx，保留原异常链。"""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    sleep_fn = sleep or time.sleep

    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as error:
            status_error = find_http_status_error(error)
            if status_error is not None:
                status = status_error.response.status_code
                is_retryable = status == 429 or status >= 500
            else:
                status = None
                timeout_error = find_http_exception(error, retry_timeout_types)
                connect_error = find_http_exception(error, (httpx.ConnectError,)) if retry_connect_errors else None
                is_retryable = timeout_error is not None or connect_error is not None
            if not is_retryable or attempt == max_attempts:
                raise
            delay = base_delay * (backoff_factor ** (attempt - 1))
            if retry_after and status_error is not None:
                header = status_error.response.headers.get("Retry-After")
                parsed_delay = retry_after_seconds(header) if header is not None else None
                if parsed_delay is not None:
                    delay = parsed_delay
            if on_retry is not None:
                on_retry(attempt, max_attempts, delay, error)
            sleep_fn(delay)

    raise RuntimeError("unreachable")
