"""LLM provider 的中立请求、响应与能力类型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

import httpx


class StructuredOutputMode(StrEnum):
    """远端结构化输出模式。"""

    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"


@dataclass(frozen=True)
class LLMCapabilities:
    """Provider 支持的结构化输出能力。"""

    json_object: bool
    json_schema_strict: bool


@dataclass(frozen=True)
class LLMMessage:
    """单条 LLM 对话消息。"""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class StructuredOutputSpec:
    """结构化输出名称、JSON Schema 与首选模式。"""

    name: str
    schema: dict[str, Any]
    preferred_mode: StructuredOutputMode


@dataclass(frozen=True)
class LLMRequest:
    """与厂商无关的 LLM 请求。"""

    messages: list[LLMMessage]
    structured_output: StructuredOutputSpec | None = None


@dataclass(frozen=True)
class LLMResponse:
    """与厂商无关的 LLM 响应。"""

    content: str | dict[str, Any]
    finish_reason: str | None
    usage_total_tokens: int
    raw_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None


class LLMProviderProtocol(Protocol):
    """LLM provider adapter 协议。"""

    name: str
    capabilities: LLMCapabilities

    def build_payload(
        self,
        model: str,
        request: LLMRequest,
        mode: StructuredOutputMode,
    ) -> dict[str, Any]: ...

    def parse_response(self, payload: dict[str, Any]) -> LLMResponse: ...

    def is_structured_mode_unsupported(self, error: httpx.HTTPStatusError) -> bool: ...
