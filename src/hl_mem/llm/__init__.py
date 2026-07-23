"""与具体厂商无关的 LLM 调用层。"""

from .client import LLMClient
from .providers import DashScopeProvider, OpenAICompatibleProvider, ZhipuProvider
from .types import (
    LLMCapabilities,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
    StructuredOutputSpec,
)

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
