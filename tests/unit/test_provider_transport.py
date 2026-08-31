from __future__ import annotations

import httpx
import pytest

from hl_mem.errors import ProviderCallError
from hl_mem.plugins.contracts import ProviderRequest
from hl_mem.plugins.transport import ProviderTransport


def _request(*, secret: str = "top-secret") -> ProviderRequest:
    return ProviderRequest(
        "POST",
        "https://provider.example.test/run",
        {"Authorization": f"Bearer {secret}"},
        {"prompt": "private memory"},
        5.0,
    )


def test_transport_marks_each_actual_retry_before_sending() -> None:
    sends = 0
    marked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if sends == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = ProviderTransport(client, sleep=lambda _delay: None).execute(
        _request(),
        max_attempts=3,
        on_attempt=marked.append,
    )

    assert response.json_body == {"ok": True}
    assert response.attempts == 2
    assert marked == [1, 2]


def test_transport_does_not_retry_400() -> None:
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(400, json={"error": {"code": "invalid"}}, request=request)

    with pytest.raises(ProviderCallError) as captured:
        ProviderTransport(
            httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _delay: None,
        ).execute(_request(), max_attempts=3, on_attempt=lambda _attempt: None)

    assert sends == 1
    assert captured.value.category == "http_error"
    assert captured.value.http_status == 400
    assert captured.value.attempts == 1
    assert captured.value.sent is True


def test_transport_retries_429_and_uses_retry_after() -> None:
    sends = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if sends == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"}, json={"code": "rate"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    response = ProviderTransport(
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    ).execute(_request(), max_attempts=2, on_attempt=lambda _attempt: None)

    assert response.attempts == 2
    assert sleeps == [0.1]


def test_transport_normalizes_exhausted_timeout_with_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderCallError) as captured:
        ProviderTransport(
            httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _delay: None,
        ).execute(_request(), max_attempts=2, on_attempt=lambda _attempt: None)

    assert captured.value.category == "http_timeout"
    assert captured.value.attempts == 2
    assert isinstance(captured.value.__cause__, httpx.ReadTimeout)


def test_transport_normalizes_invalid_json_after_send() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", request=request)

    with pytest.raises(ProviderCallError) as captured:
        ProviderTransport(httpx.Client(transport=httpx.MockTransport(handler))).execute(
            _request(), max_attempts=1, on_attempt=lambda _attempt: None
        )

    assert captured.value.category == "invalid_response"
    assert captured.value.attempts == 1


def test_transport_redacts_request_secrets_from_normalized_error() -> None:
    secret = "plain-secret-value-123"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={"x-request-id": "request-1"},
            json={"error": {"code": "auth", "message": secret}},
            request=request,
        )

    with pytest.raises(ProviderCallError) as captured:
        ProviderTransport(httpx.Client(transport=httpx.MockTransport(handler))).execute(
            _request(secret=secret), max_attempts=1, on_attempt=lambda _attempt: None
        )

    error = captured.value
    assert error.category == "auth"
    assert error.request_id == "request-1"
    assert secret not in str(error)
    assert secret not in str(error.response_body)


def test_mark_failure_happens_before_network_and_is_not_retried() -> None:
    sends = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    def fail_mark(_attempt: int) -> None:
        raise RuntimeError("ledger unavailable")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        ProviderTransport(httpx.Client(transport=httpx.MockTransport(handler))).execute(
            _request(), max_attempts=3, on_attempt=fail_mark
        )
    assert sends == 0
