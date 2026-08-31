from __future__ import annotations

import httpx
import pytest

from hl_mem.http_utils import retry_http


def _status_error(status: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example.test/run")
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("failed", request=request, response=response)


def test_retry_http_retries_429_and_honors_retry_after() -> None:
    attempts = 0
    sleeps: list[float] = []

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _status_error(429, retry_after="0.25")
        return "ok"

    assert retry_http(call, retry_after=True, sleep=sleeps.append) == "ok"
    assert attempts == 2
    assert sleeps == [0.25]


def test_retry_http_does_not_retry_non_retryable_4xx() -> None:
    attempts = 0

    def call() -> None:
        nonlocal attempts
        attempts += 1
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        retry_http(call, sleep=lambda _delay: None)
    assert attempts == 1


def test_retry_http_retries_timeouts_only_to_the_bound() -> None:
    attempts = 0

    def call() -> None:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout):
        retry_http(call, max_attempts=2, sleep=lambda _delay: None)
    assert attempts == 2
