"""Transport-neutral LLM contracts with lazy implementation exports."""

from __future__ import annotations

from typing import Any

from .types import (
    LLMCapabilities,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
    StructuredOutputSpec,
)


def __getattr__(name: str) -> Any:
    if name == "LLMClient":
        from .client import LLMClient

        return LLMClient
    if name in {"DashScopeProvider", "OpenAICompatibleProvider", "ZhipuProvider"}:
        from . import providers

        return getattr(providers, name)
    raise AttributeError(name)


__all__ = [
    "DashScopeProvider",
    "LLMCapabilities",
    "LLMClient",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "StructuredOutputMode",
    "StructuredOutputSpec",
    "ZhipuProvider",
]
