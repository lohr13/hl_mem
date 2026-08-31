"""由宿主独占的 Provider HTTP 传输、重试与错误归一化。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from hl_mem.errors import ProviderCallError
from hl_mem.http_utils import http_error_diagnostics, retry_http, sanitize_http_response_body
from hl_mem.plugins.contracts import ProviderRequest, ProviderResponse


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _classify(error: Exception, provider_code: str | None) -> tuple[str, int | None]:
    if isinstance(error, httpx.TimeoutException):
        return "http_timeout", None
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429:
            return ("quota" if provider_code and "quota" in provider_code.casefold() else "rate_limit"), status
        if status in {401, 403}:
            return "auth", status
        if status >= 500:
            return "upstream", status
        return "http_error", status
    if isinstance(error, httpx.RequestError):
        return "upstream", None
    return "provider_error", None


def _request_secrets(request: ProviderRequest) -> tuple[str, ...]:
    secret_values: list[str] = []
    for raw_value in request.headers.values():
        value = str(raw_value)
        secret_values.append(value)
        if value.casefold().startswith("bearer "):
            secret_values.append(value[7:])
    return tuple(secret_values)


class ProviderTransport:
    """执行稳定 Provider 能力的唯一网络边界。"""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._sleep = sleep

    def execute(
        self,
        request: ProviderRequest,
        *,
        max_attempts: int,
        on_attempt: Callable[[int], None],
    ) -> ProviderResponse:
        attempts = 0

        def send() -> httpx.Response:
            nonlocal attempts
            next_attempt = attempts + 1
            on_attempt(next_attempt)
            attempts = next_attempt
            sender = self._client.request if self._client is not None else httpx.request
            response = sender(
                request.method,
                request.url,
                headers=dict(request.headers),
                json=_thaw(request.json_body),
                timeout=request.timeout_seconds,
            )
            response.raise_for_status()
            return response

        try:
            response = retry_http(
                send,
                max_attempts=max_attempts,
                retry_after=True,
                sleep=self._sleep,
            )
        except Exception as error:
            if attempts == 0:
                raise
            secrets = _request_secrets(request)
            diagnostics = http_error_diagnostics(error, secrets=secrets) or {}
            provider_code = diagnostics.get("provider_code")
            normalized_code = str(provider_code) if provider_code is not None else None
            category, status = _classify(error, normalized_code)
            normalized = ProviderCallError(
                category,
                f"Provider HTTP call failed after {attempts} attempt(s): {category}",
                attempts=attempts,
                sent=True,
                http_status=status,
                provider_code=normalized_code,
                request_id=(str(diagnostics["request_id"]) if diagnostics.get("request_id") is not None else None),
                response_body=diagnostics.get("response_body"),
            )
            raise normalized from error

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("Provider response body must be a JSON object")
        except Exception as error:
            normalized = ProviderCallError(
                "invalid_response",
                f"Provider returned an invalid JSON response after {attempts} attempt(s)",
                attempts=attempts,
                sent=True,
                http_status=response.status_code,
                request_id=response.headers.get("x-request-id"),
            )
            raise normalized from error
        request_id = next(
            (
                response.headers.get(header)
                for header in ("x-request-id", "x-dashscope-request-id", "request-id")
                if response.headers.get(header)
            ),
            None,
        )
        if request_id is None:
            raw_request_id = payload.get("request_id") or payload.get("requestId") or payload.get("id")
            request_id = str(raw_request_id) if raw_request_id is not None else None
        if request_id is not None:
            request_id = sanitize_http_response_body(
                request_id,
                limit=256,
                secrets=_request_secrets(request),
            )
        return ProviderResponse(
            response.status_code,
            dict(response.headers),
            payload,
            attempts,
            request_id,
        )


__all__ = ["ProviderTransport"]
