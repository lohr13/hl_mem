"""同步 LLM transport、HTTP 重试与 structured output 降级。"""

from __future__ import annotations

from typing import Any

import httpx

from hl_mem.errors import LLMStructuredOutputUnsupportedError
from hl_mem.http_utils import retry_http
from hl_mem.observability.audit import current_audit

from .types import (
    LLMProviderProtocol,
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
)


class LLMClient:
    """执行与 provider 无关的同步 LLM 请求。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        provider: LLMProviderProtocol,
        timeout: httpx.Timeout,
        max_attempts: int,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._client = client
        self._strict_unsupported = False

    def complete(self, request: LLMRequest) -> LLMResponse:
        """完成一次 LLM 调用，并按 provider 能力选择或降级结构化模式。"""
        mode = self._select_structured_mode(request)
        try:
            return self._complete_with_mode(request, mode)
        except httpx.HTTPStatusError as error:
            should_fallback = (
                request.structured_output is not None
                and mode is StructuredOutputMode.JSON_SCHEMA
                and self.provider.is_structured_mode_unsupported(error)
            )
            if not should_fallback:
                raise
            if not self.provider.capabilities.json_object:
                raise LLMStructuredOutputUnsupportedError(
                    f"Provider {self.provider.name} does not support requested structured output"
                ) from error
            self._strict_unsupported = True
            current_audit().emit(
                "llm",
                "structured_fallback",
                "structured_fallback",
                detail={"provider": self.provider.name, "model": self.model},
            )
            return self._complete_with_mode(request, StructuredOutputMode.JSON_OBJECT)

    def _complete_with_mode(
        self,
        request: LLMRequest,
        mode: StructuredOutputMode,
    ) -> LLMResponse:
        payload = self.provider.build_payload(self.model, request, mode)
        response_payload = retry_http(
            lambda: self._post_once(payload),
            max_attempts=self.max_attempts,
        )
        return self.provider.parse_response(response_payload)

    def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送一次 Chat Completions 请求并解析 JSON 外壳。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        post = self._client.post if self._client is not None else httpx.post
        response = post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def _select_structured_mode(self, request: LLMRequest) -> StructuredOutputMode:
        """根据请求偏好、能力和已缓存降级状态选择结构化模式。"""
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
            f"Provider {self.provider.name} has no supported structured output mode"
        )
