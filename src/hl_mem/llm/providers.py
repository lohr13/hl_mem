"""OpenAI Chat Completions 兼容 provider adapter。"""

from __future__ import annotations

from typing import Any

import httpx

from .types import (
    LLMCapabilities,
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
)


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions adapter。"""

    name = "openai_compatible"
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=True)

    def build_payload(
        self,
        model: str,
        request: LLMRequest,
        mode: StructuredOutputMode,
    ) -> dict[str, Any]:
        """构建 OpenAI-compatible 请求体。"""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
        }
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

    def parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        """解析 OpenAI-compatible 响应外壳。"""
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        return LLMResponse(
            content=choice["message"]["content"],
            finish_reason=choice.get("finish_reason"),
            usage_total_tokens=int(usage.get("total_tokens", 0)),
            raw_request_id=payload.get("id") or payload.get("request_id"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_tokens=prompt_details.get("cached_tokens"),
        )

    def is_structured_mode_unsupported(self, error: httpx.HTTPStatusError) -> bool:
        """判断 400/422 是否明确表示 strict structured output 不受支持。"""
        response = error.response
        if response is None or response.status_code not in {400, 422}:
            return False
        text = response.text.casefold()
        return any(marker in text for marker in ("response_format", "json_schema", "strict"))


class DashScopeProvider(OpenAICompatibleProvider):
    """百炼 Qwen OpenAI-compatible adapter。"""

    name = "dashscope"
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)


class ZhipuProvider(OpenAICompatibleProvider):
    """智谱 GLM OpenAI-compatible adapter。"""

    name = "zhipu"
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)
