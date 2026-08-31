"""Provider 插件的版本化、传输中立公共契约。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from re import fullmatch
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

from hl_mem.errors import PluginManifestError, ProviderCallError
from hl_mem.llm.types import LLMCapabilities, LLMRequest, LLMResponse, StructuredOutputMode

PROVIDER_API_VERSION: Final[int] = 1
PROVIDER_ENTRY_POINT_GROUP: Final[str] = "hl_mem.providers"

_PROVIDER_NAME_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


class ProviderCapability(StrEnum):
    """宿主支持的 Provider 能力种类。"""

    LLM = "llm"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    IMAGE_DESCRIBER = "image_describer"


class ProviderStability(StrEnum):
    """能力契约的稳定性等级。"""

    STABLE = "stable"
    EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class ProviderKey:
    capability: ProviderCapability
    name: str

    def __post_init__(self) -> None:
        if fullmatch(_PROVIDER_NAME_PATTERN, self.name) is None:
            raise ValueError(f"invalid provider name: {self.name!r}")


@dataclass(frozen=True)
class ProviderEndpoint:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float
    max_attempts: int
    connect_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("provider base_url must not be empty")
        if not self.model.strip():
            raise ValueError("provider model must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        if self.connect_timeout_seconds is not None and self.connect_timeout_seconds <= 0:
            raise ValueError("provider connect timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("provider max_attempts must be at least 1")


@dataclass(frozen=True)
class ProviderRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    json_body: Mapping[str, Any] = field(repr=False)
    timeout_seconds: float
    connect_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.method.strip():
            raise ValueError("provider request method must not be empty")
        if not self.url.strip():
            raise ValueError("provider request URL must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("provider request timeout must be positive")
        if self.connect_timeout_seconds is not None and self.connect_timeout_seconds <= 0:
            raise ValueError("provider request connect timeout must be positive")
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "headers", _freeze(self.headers))
        object.__setattr__(self, "json_body", _freeze(self.json_body))


@dataclass(frozen=True)
class ProviderResponse:
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    json_body: Mapping[str, Any] = field(repr=False)
    attempts: int
    request_id: str | None

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("provider response status_code must be an HTTP status")
        if self.attempts < 1:
            raise ValueError("provider response attempts must be at least 1")
        object.__setattr__(self, "headers", _freeze(self.headers))
        object.__setattr__(self, "json_body", _freeze(self.json_body))


@dataclass(frozen=True)
class ProviderFactoryContext:
    key: ProviderKey
    core_options: Mapping[str, Any]
    plugin_options: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "core_options", _freeze(self.core_options))
        object.__setattr__(self, "plugin_options", _freeze(self.plugin_options))


@dataclass(frozen=True)
class EmbeddingInvocation:
    texts: tuple[str, ...]
    dimensions: int | None
    api_mode: str
    text_type: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "texts", tuple(self.texts))


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vectors", tuple(tuple(vector) for vector in self.vectors))


@dataclass(frozen=True)
class RerankInvocation:
    query: str
    documents: tuple[str, ...]
    top_n: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "documents", tuple(self.documents))


@dataclass(frozen=True)
class RerankResult:
    results: tuple[tuple[int, float], ...]
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "results", tuple(tuple(result) for result in self.results))


@dataclass(frozen=True)
class ValidatedImageInput:
    data: bytes = field(repr=False)
    media_type: str
    sha256: str


@dataclass(frozen=True)
class ImageProviderResult:
    caption: str
    ocr_text: str | None
    model: str
    confidence: float | None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class LLMInvocation:
    request: LLMRequest
    mode: StructuredOutputMode
    max_tokens: int | None = None
    enable_thinking: bool = False
    thinking_control: str = "auto"
    reasoning_effort: str | None = None


@runtime_checkable
class LLMProviderAdapter(Protocol):
    capabilities: LLMCapabilities

    def build_request(self, endpoint: ProviderEndpoint, invocation: LLMInvocation) -> ProviderRequest: ...

    def parse_response(self, response: ProviderResponse) -> LLMResponse: ...

    def is_structured_mode_unsupported(self, error: ProviderCallError) -> bool: ...


@runtime_checkable
class EmbeddingProviderAdapter(Protocol):
    def build_request(self, endpoint: ProviderEndpoint, invocation: EmbeddingInvocation) -> ProviderRequest: ...

    def parse_response(self, response: ProviderResponse) -> EmbeddingResult: ...


@runtime_checkable
class RerankerProviderAdapter(Protocol):
    def build_request(self, endpoint: ProviderEndpoint, invocation: RerankInvocation) -> ProviderRequest: ...

    def parse_response(self, response: ProviderResponse) -> RerankResult: ...


@runtime_checkable
class ImageProviderAdapter(Protocol):
    def build_request(self, endpoint: ProviderEndpoint, image: ValidatedImageInput) -> ProviderRequest: ...

    def parse_response(self, response: ProviderResponse) -> ImageProviderResult: ...


ProviderFactory = Callable[[ProviderFactoryContext], object]


@dataclass(frozen=True)
class ProviderCapabilitySpec:
    name: str
    capability: ProviderCapability
    stability: ProviderStability

    @property
    def key(self) -> ProviderKey:
        return ProviderKey(self.capability, self.name)


@dataclass(frozen=True)
class ProviderManifest:
    id: str
    version: str
    api_version: int
    requires_hl_mem: str
    capabilities: tuple[ProviderCapabilitySpec, ...]
    config_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "config_schema", _freeze(self.config_schema))


@dataclass(frozen=True)
class ProviderPlugin:
    manifest: ProviderManifest
    factories: Mapping[ProviderKey, ProviderFactory]

    def __post_init__(self) -> None:
        factories = MappingProxyType(dict(self.factories))
        expected = {item.key for item in self.manifest.capabilities}
        actual = set(factories)
        if actual != expected:
            raise PluginManifestError(
                f"provider factory keys do not match manifest capabilities: expected {sorted(map(str, expected))}, "
                f"got {sorted(map(str, actual))}"
            )
        object.__setattr__(self, "factories", factories)


__all__ = [
    "PROVIDER_API_VERSION",
    "PROVIDER_ENTRY_POINT_GROUP",
    "EmbeddingInvocation",
    "EmbeddingProviderAdapter",
    "EmbeddingResult",
    "ImageProviderAdapter",
    "ImageProviderResult",
    "LLMInvocation",
    "LLMProviderAdapter",
    "ProviderCapability",
    "ProviderCapabilitySpec",
    "ProviderEndpoint",
    "ProviderFactory",
    "ProviderFactoryContext",
    "ProviderKey",
    "ProviderManifest",
    "ProviderPlugin",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderStability",
    "RerankInvocation",
    "RerankResult",
    "RerankerProviderAdapter",
    "ValidatedImageInput",
]
