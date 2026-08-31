"""Built-in neutral adapters for OpenAI-compatible Chat Completions APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import httpx

from hl_mem.errors import ProviderCallError
from hl_mem.plugins.contracts import (
    LLMInvocation,
    ProviderEndpoint,
    ProviderFactoryContext,
    ProviderRequest,
    ProviderResponse,
)

from .types import LLMCapabilities, LLMRequest, LLMResponse, StructuredOutputMode


class OpenAICompatibleProvider:
    """Translate the neutral LLM contract to Chat Completions."""

    name = "openai_compatible"
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=True)

    def __init__(
        self,
        *,
        max_tokens: int | None = None,
        enable_thinking: bool = False,
        thinking_control: Literal["auto", "chat_template_kwargs"] = "auto",
    ) -> None:
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.thinking_control = thinking_control
        if thinking_control == "chat_template_kwargs":
            self.capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)

    def build_payload(
        self,
        model: str,
        request: LLMRequest,
        mode: StructuredOutputMode,
    ) -> dict[str, Any]:
        """Build the vendor payload; retained as a small characterization seam."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.thinking_control == "chat_template_kwargs":
            payload["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        spec = request.structured_output
        if spec is None:
            return payload
        if mode is StructuredOutputMode.JSON_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": spec.name,
                    "schema": spec.schema,
                    "strict": True,
                },
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def build_request(self, endpoint: ProviderEndpoint, invocation: LLMInvocation) -> ProviderRequest:
        return ProviderRequest(
            "POST",
            f"{endpoint.base_url.rstrip('/')}/chat/completions",
            {
                "Authorization": f"Bearer {endpoint.api_key}",
                "Content-Type": "application/json",
            },
            self.build_payload(endpoint.model, invocation.request, invocation.mode),
            endpoint.timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse | Mapping[str, Any]) -> LLMResponse:
        payload = response.json_body if isinstance(response, ProviderResponse) else response
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        content = choice["message"]["content"]
        if self.thinking_control == "chat_template_kwargs":
            content = self._strip_leading_empty_think_block(content)
        request_id = response.request_id if isinstance(response, ProviderResponse) else None
        return LLMResponse(
            content=content,
            finish_reason=choice.get("finish_reason"),
            usage_total_tokens=int(usage.get("total_tokens", 0)),
            raw_request_id=request_id or payload.get("id") or payload.get("request_id"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_tokens=prompt_details.get("cached_tokens"),
        )

    @staticmethod
    def _strip_leading_empty_think_block(content: str) -> str:
        opening_tag = "<think>"
        closing_tag = "</think>"
        if not content.startswith(opening_tag):
            return content
        closing_index = content.find(closing_tag, len(opening_tag))
        if closing_index < 0 or content[len(opening_tag) : closing_index].strip():
            return content
        remainder = content[closing_index + len(closing_tag) :].lstrip()
        return remainder if remainder.startswith("{") else content

    def is_structured_mode_unsupported(self, error: ProviderCallError | httpx.HTTPStatusError) -> bool:
        if isinstance(error, ProviderCallError):
            if error.http_status not in {400, 422}:
                return False
            text = str(error.response_body or "").casefold()
        else:
            response = error.response
            if response is None or response.status_code not in {400, 422}:
                return False
            text = response.text.casefold()
        return any(marker in text for marker in ("response_format", "json_schema", "strict"))


class DashScopeProvider(OpenAICompatibleProvider):
    name = "dashscope"
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)

    def __init__(self, *, enable_thinking: bool = False, max_tokens: int | None = None) -> None:
        super().__init__(max_tokens=max_tokens)
        self.enable_thinking = enable_thinking

    def build_payload(self, model: str, request: LLMRequest, mode: StructuredOutputMode) -> dict[str, Any]:
        payload = super().build_payload(model, request, mode)
        payload["enable_thinking"] = self.enable_thinking
        return payload


class ZhipuProvider(OpenAICompatibleProvider):
    name = "zhipu"
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)

    def __init__(self, *, max_tokens: int | None = None, reasoning_effort: str | None = None) -> None:
        if reasoning_effort not in {None, "low", "high", "max"}:
            raise ValueError("reasoning_effort must be 'low', 'high', 'max', or None")
        super().__init__(max_tokens=max_tokens)
        self.reasoning_effort = reasoning_effort

    def build_payload(self, model: str, request: LLMRequest, mode: StructuredOutputMode) -> dict[str, Any]:
        payload = super().build_payload(model, request, mode)
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        return payload


def make_builtin_llm_provider(context: ProviderFactoryContext) -> OpenAICompatibleProvider:
    """Create a built-in adapter only from validated non-secret core options."""
    options = context.core_options
    max_tokens = options.get("max_tokens")
    if context.key.name == "dashscope":
        return DashScopeProvider(
            enable_thinking=bool(options.get("enable_thinking", False)),
            max_tokens=max_tokens,
        )
    if context.key.name == "zhipu":
        reasoning_effort = options.get("reasoning_effort")
        return ZhipuProvider(
            max_tokens=max_tokens,
            reasoning_effort=str(reasoning_effort) if reasoning_effort is not None else None,
        )
    if context.key.name == "openai_compatible":
        return OpenAICompatibleProvider(
            max_tokens=max_tokens,
            enable_thinking=bool(options.get("enable_thinking", False)),
            thinking_control=str(options.get("thinking_control", "auto")),  # type: ignore[arg-type]
        )
    raise ValueError(f"unsupported built-in LLM provider {context.key.name!r}")


__all__ = [
    "DashScopeProvider",
    "OpenAICompatibleProvider",
    "ZhipuProvider",
    "make_builtin_llm_provider",
]
