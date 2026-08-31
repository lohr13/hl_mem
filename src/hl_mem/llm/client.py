"""Governed, transport-neutral LLM completion client."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import httpx

from hl_mem.errors import (
    LLMStructuredOutputUnsupportedError,
    ProviderCallError,
)
from hl_mem.observability.audit import current_audit
from hl_mem.observability.llm_spans import LLMSpanRecorder
from hl_mem.observability.usage import UsageAmount
from hl_mem.plugins.contracts import (
    LLMInvocation,
    LLMProviderAdapter,
    ProviderEndpoint,
    ProviderResponse,
)
from hl_mem.plugins.proxies import GovernedProviderCall

from .types import LLMRequest, LLMResponse, StructuredOutputMode


def classify_provider_error(error: Exception) -> tuple[str, int | None, str | None]:
    """Normalize both governed and legacy HTTP errors for logical LLM spans."""
    if isinstance(error, ProviderCallError):
        return error.category, error.http_status, error.provider_code
    if isinstance(error, httpx.TimeoutException):
        return "http_timeout", None, None
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        provider_code: str | None = None
        try:
            body = error.response.json()
            if isinstance(body, dict):
                raw_code = body.get("code")
                raw_error = body.get("error")
                if raw_code is None and isinstance(raw_error, dict):
                    raw_code = raw_error.get("code")
                provider_code = str(raw_code) if raw_code else None
        except (TypeError, ValueError):
            pass
        if status == 429:
            category = "quota" if provider_code and "quota" in provider_code.lower() else "rate_limit"
        elif status in {401, 403}:
            category = "auth"
        elif status >= 500:
            category = "upstream"
        else:
            category = "http_error"
        return category, status, provider_code
    if isinstance(error, httpx.RequestError):
        return "upstream", None, None
    return type(error).__name__, None, None


@dataclass(frozen=True)
class LLMClientOptions:
    """Generation controls applied consistently to each logical completion."""

    max_tokens: int | None = None
    enable_thinking: bool = False
    thinking_control: str = "auto"
    reasoning_effort: str | None = None


class LLMClient:
    """Preserve the logical completion API while governing every HTTP sequence."""

    def __init__(
        self,
        *,
        endpoint: ProviderEndpoint,
        provider_name: str,
        provider: LLMProviderAdapter,
        governed: GovernedProviderCall[LLMResponse],
        options: LLMClientOptions | None = None,
        span_recorder: LLMSpanRecorder | None = None,
        operation: str = "other",
        owned_runtime: object | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = endpoint.api_key
        self.base_url = endpoint.base_url.rstrip("/")
        self.model = endpoint.model
        self.provider_name = provider_name
        self.provider = provider
        self.timeout = httpx.Timeout(endpoint.timeout_seconds)
        self.max_attempts = endpoint.max_attempts
        self._governed = governed
        resolved_options = options or LLMClientOptions()
        self._max_tokens = resolved_options.max_tokens
        self._enable_thinking = resolved_options.enable_thinking
        self._thinking_control = resolved_options.thinking_control
        self._reasoning_effort = resolved_options.reasoning_effort
        self._span_recorder = span_recorder
        self._operation = operation
        self._owned_runtime = owned_runtime
        self._strict_unsupported = False

    def close(self) -> None:
        runtime = self._owned_runtime
        if runtime is not None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
            self._owned_runtime = None

    def complete(self, request: LLMRequest, *, timeout_seconds: float | None = None) -> LLMResponse:
        mode = self._select_structured_mode(request)
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        try:
            response = self._complete_with_mode(request, mode, timeout_seconds)
        except ProviderCallError as error:
            should_fallback = (
                request.structured_output is not None
                and mode is StructuredOutputMode.JSON_SCHEMA
                and self.provider.is_structured_mode_unsupported(error)
            )
            if not should_fallback:
                self._record_span(mode, "error", started_at, started, error=error)
                raise
            if not self.provider.capabilities.json_object:
                self._record_span(mode, "error", started_at, started, error=error)
                raise LLMStructuredOutputUnsupportedError(
                    f"Provider {self.provider_name} does not support requested structured output"
                ) from error
            self._strict_unsupported = True
            current_audit().emit(
                "llm",
                "structured_fallback",
                "structured_fallback",
                detail={"provider": self.provider_name, "model": self.model},
            )
            try:
                response = self._complete_with_mode(request, StructuredOutputMode.JSON_OBJECT, timeout_seconds)
            except Exception as fallback_error:
                self._record_span(
                    StructuredOutputMode.JSON_OBJECT,
                    "error",
                    started_at,
                    started,
                    error=fallback_error,
                )
                raise
            mode = StructuredOutputMode.JSON_OBJECT
        except Exception as error:
            self._record_span(mode, "error", started_at, started, error=error)
            raise
        self._record_span(mode, "success", started_at, started, response=response)
        return response

    def _record_span(
        self,
        mode: StructuredOutputMode,
        status: str,
        started_at: str,
        started: float,
        *,
        response: LLMResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        if self._span_recorder is None:
            return
        self._span_recorder.record(
            operation=self._operation,
            provider=self.provider_name,
            model=self.model,
            structured_mode=mode.value,
            status=status,
            error_class=classify_provider_error(error)[0] if error is not None else None,
            raw_request_id=response.raw_request_id if response is not None else None,
            input_tokens=response.input_tokens if response is not None else None,
            output_tokens=response.output_tokens if response is not None else None,
            cached_tokens=response.cached_tokens if response is not None else None,
            total_tokens=response.usage_total_tokens if response is not None else None,
            latency_ms=(time.perf_counter() - started) * 1000,
            started_at=started_at,
        )

    def _complete_with_mode(
        self,
        request: LLMRequest,
        mode: StructuredOutputMode,
        timeout_seconds: float | None = None,
    ) -> LLMResponse:
        endpoint = self.endpoint if timeout_seconds is None else replace(self.endpoint, timeout_seconds=timeout_seconds)
        invocation = LLMInvocation(
            request=request,
            mode=mode,
            max_tokens=self._max_tokens,
            enable_thinking=self._enable_thinking,
            thinking_control=self._thinking_control,
            reasoning_effort=self._reasoning_effort,
        )
        estimate = self._estimate_usage(request)

        def parse(response: ProviderResponse) -> tuple[LLMResponse, UsageAmount]:
            value = self.provider.parse_response(response)
            return value, self._actual_usage(value, estimate)

        return self._governed.execute_factory(
            lambda: self.provider.build_request(endpoint, invocation),
            estimate,
            parse,
            max_attempts=endpoint.max_attempts,
            settlement_status=self._settlement_status,
        )

    @staticmethod
    def _settlement_status(response: LLMResponse) -> str:
        if response.input_tokens is None and response.output_tokens is None and response.usage_total_tokens <= 0:
            return "estimated"
        return "success"

    def _estimate_usage(self, request: LLMRequest) -> UsageAmount:
        input_tokens = max(1, sum(len(message.content) for message in request.messages) // 2)
        output_tokens = self._max_tokens if self._max_tokens is not None else max(256, min(4096, input_tokens))
        return UsageAmount(requests=1, input_tokens=input_tokens, output_tokens=output_tokens)

    @staticmethod
    def _actual_usage(response: LLMResponse, estimate: UsageAmount) -> UsageAmount:
        if response.input_tokens is not None or response.output_tokens is not None:
            input_tokens = response.input_tokens if response.input_tokens is not None else estimate.input_tokens
            output_tokens = response.output_tokens if response.output_tokens is not None else estimate.output_tokens
            return UsageAmount(requests=1, input_tokens=input_tokens, output_tokens=output_tokens)
        if response.usage_total_tokens > 0:
            return UsageAmount(requests=1, input_tokens=response.usage_total_tokens)
        return estimate

    def _select_structured_mode(self, request: LLMRequest) -> StructuredOutputMode:
        spec = request.structured_output
        if spec is None:
            return StructuredOutputMode.JSON_OBJECT
        if (
            spec.preferred_mode is StructuredOutputMode.JSON_SCHEMA
            and self.provider.capabilities.json_schema_strict
            and not self._strict_unsupported
        ):
            return StructuredOutputMode.JSON_SCHEMA
        if self.provider.capabilities.json_object:
            return StructuredOutputMode.JSON_OBJECT
        raise LLMStructuredOutputUnsupportedError(
            f"Provider {self.provider_name} has no supported structured output mode"
        )


__all__ = ["LLMClient", "classify_provider_error"]
