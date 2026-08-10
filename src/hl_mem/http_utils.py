"""统一的 HTTP 重试策略。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

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


def sanitize_http_response_body(body: str, *, limit: int = 500, secrets: Iterable[str] = ()) -> str:
    """Redact common credentials before retaining a bounded provider response."""
    if limit < 0:
        raise ValueError("HTTP response body limit must be non-negative")
    sanitized = body
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = re.sub(
        r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s,\"']+",
        r"\1[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret)[\"']?\s*[:=]\s*[\"']?)[^\s,\"']+",
        r"\1[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-[REDACTED]", sanitized)
    return sanitized[:limit]


def _provider_error_fields(
    response: httpx.Response,
    *,
    secrets: Iterable[str] = (),
) -> tuple[str | None, str | None]:
    try:
        payload = response.json()
    except (TypeError, ValueError, httpx.ResponseNotRead):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    raw_error = payload.get("error")
    error_payload = raw_error if isinstance(raw_error, dict) else {}
    raw_code = payload.get("code") or error_payload.get("code")
    raw_request_id = payload.get("request_id") or payload.get("requestId") or error_payload.get("request_id")
    provider_code = (
        sanitize_http_response_body(str(raw_code), limit=128, secrets=secrets) if raw_code is not None else None
    )
    request_id = (
        sanitize_http_response_body(str(raw_request_id), limit=256, secrets=secrets)
        if raw_request_id is not None
        else None
    )
    return provider_code or None, request_id or None


def http_error_diagnostics(
    error: BaseException,
    *,
    body_limit: int = 500,
    secrets: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Return safe diagnostics for the first HTTP status error in a cause chain."""
    status_error = find_http_status_error(error)
    if status_error is None:
        return None
    response = status_error.response
    secret_values = tuple(value for value in secrets if value)
    provider_code, body_request_id = _provider_error_fields(response, secrets=secret_values)
    header_request_id = next(
        (
            response.headers.get(header)
            for header in ("x-request-id", "x-dashscope-request-id", "request-id")
            if response.headers.get(header)
        ),
        None,
    )
    request_id = (
        sanitize_http_response_body(str(header_request_id), limit=256, secrets=secret_values)
        if header_request_id
        else body_request_id
    )
    try:
        response_body = sanitize_http_response_body(response.text, limit=body_limit, secrets=secret_values)
    except httpx.ResponseNotRead:
        response_body = None
    return {
        "http_status": response.status_code,
        "provider_code": provider_code,
        "request_id": request_id or None,
        "response_body": response_body,
    }


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
