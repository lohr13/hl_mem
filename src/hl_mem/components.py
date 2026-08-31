"""Composition root for configured HL-Mem runtime components."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from hl_mem.domain.entity import load_entity_aliases, set_active_aliases
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import Embedder, FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.ingest.image_describer import DashScopeImageDescriber
from hl_mem.ingest.llm_extractor import ExtractionModes, LLMExtractor
from hl_mem.ingest.verifier import EntailmentVerifier
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import StructuredOutputMode
from hl_mem.observability.llm_spans import LLMSpanRecorder
from hl_mem.plugins.contracts import ProviderCapability, ProviderEndpoint
from hl_mem.plugins.registry import ProviderRegistry, build_provider_registry
from hl_mem.plugins.runtime import ProviderRuntime, create_provider_runtime
from hl_mem.protocols import (
    EmbedderProtocol,
    ExtractorProtocol,
    ImageDescriberProtocol,
    RelationDiscoveryProtocol,
    RerankerProtocol,
)
from hl_mem.recall.query_expansion import QueryExpander
from hl_mem.recall.reranker import DashScopeReranker
from hl_mem.recall.reranker import make_reranker as make_registered_reranker
from hl_mem.settings import Settings

_EXTRACTOR_REGISTRY: dict[str, str] = {
    "message": "llm",
    "explicit_memory": "explicit",
    "tool_result": "llm",
}

Reranker = DashScopeReranker
_COMPONENT_HEALTH: dict[str, dict[str, str | None]] = {}


def make_provider_registry(settings: Settings, *, entry_points: Any = None) -> ProviderRegistry:
    """Build one validated, frozen Provider registry."""
    return build_provider_registry(settings, entry_points=entry_points)


def initialize_process(settings: Settings) -> None:
    """Apply explicit process-wide immutable domain configuration."""
    set_active_aliases(load_entity_aliases(settings.entity_aliases_path))


def component_health() -> dict[str, dict[str, str | None]]:
    return {name: dict(status) for name, status in _COMPONENT_HEALTH.items()}


def _record_component_health(
    name: str,
    requested_mode: str,
    effective_mode: str,
    degradation_reason: str | None = None,
) -> None:
    _COMPONENT_HEALTH[name] = {
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "degradation_reason": degradation_reason,
    }


def make_image_describer(settings: Settings) -> ImageDescriberProtocol | None:
    """Build the experimental image component only when explicitly enabled."""
    if settings.image_describer_mode == "off":
        return None
    if not settings.image_describer_api_key:
        raise ConfigurationError("IMAGE_API_KEY is required")
    return DashScopeImageDescriber(
        settings.image_describer_api_key,
        settings.image_describer_base_url,
        settings.image_describer_model,
        max_bytes=settings.image_max_bytes,
        allow_file_uris=settings.image_allow_file_uris,
        file_allow_roots=tuple(Path(root) for root in settings.image_file_allow_roots),
        max_attempts=settings.llm_max_attempts,
    )


def make_llm_client(
    settings: Settings,
    connection: sqlite3.Connection | None = None,
    *,
    operation: str = "other",
    model: str | None = None,
    provider_name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    span_recorder: Any = None,
    runtime: ProviderRuntime | None = None,
) -> LLMClient:
    """Resolve an LLM adapter and wrap it in host-owned governance."""
    resolved_api_key = api_key if api_key is not None else settings.llm_api_key
    if not resolved_api_key:
        raise ConfigurationError("LLM_API_KEY is required")
    selected_provider = provider_name if provider_name is not None else settings.llm_provider
    resolved_runtime = runtime or create_provider_runtime(settings)
    provider = resolved_runtime.registry.create_llm(
        selected_provider,
        {
            "max_tokens": settings.llm_max_tokens,
            "enable_thinking": settings.enable_llm_thinking,
            "thinking_control": settings.llm_thinking_control,
            "reasoning_effort": settings.llm_reasoning_effort,
        },
    )
    normalized_model = model.strip() if model is not None else None
    endpoint = ProviderEndpoint(
        base_url=base_url if base_url is not None else settings.llm_base_url,
        api_key=resolved_api_key,
        model=normalized_model or settings.llm_model,
        timeout_seconds=settings.llm_timeout,
        max_attempts=settings.llm_max_attempts,
    )
    return LLMClient(
        endpoint=endpoint,
        provider_name=selected_provider,
        provider=provider,
        governed=resolved_runtime.governed_call(
            ProviderCapability.LLM,
            selected_provider,
            operation,
            endpoint.model,
        ),
        max_tokens=settings.llm_max_tokens,
        enable_thinking=settings.enable_llm_thinking,
        thinking_control=settings.llm_thinking_control,
        reasoning_effort=settings.llm_reasoning_effort,
        span_recorder=span_recorder if span_recorder is not None else LLMSpanRecorder(connection),
        operation=operation,
        owned_runtime=resolved_runtime if runtime is None else None,
    )


def make_embedder(settings: Settings) -> EmbedderProtocol:
    if settings.embedder_mode == "fake":
        return FakeEmbedder(settings.embedding_dim)
    if not settings.embedding_api_key:
        raise ConfigurationError("HL_MEM_EMBEDDER=real but EMBEDDING_API_KEY is missing")
    embedder_options: dict[str, Any] = {"api_mode": settings.embedding_api_mode}
    if settings.embedding_text_type:
        embedder_options["text_type"] = settings.embedding_text_type
    return Embedder(
        settings.embedding_api_key,
        settings.embedding_base_url,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_connect_timeout,
        settings.embedding_read_timeout,
        settings.embedding_max_attempts,
        **embedder_options,
    )


def make_reranker(settings: Settings) -> RerankerProtocol | None:
    return make_registered_reranker(settings, {"dashscope": Reranker})


def make_query_expander(
    settings: Settings,
    connection: sqlite3.Connection | None = None,
    *,
    span_recorder: Any = None,
    runtime: ProviderRuntime | None = None,
) -> QueryExpander | None:
    line_overrides = settings.query_expansion_line_overrides()
    if settings.query_expansion_mode == "off" or settings.query_expansion_max == 0:
        _record_component_health("query_expander", settings.query_expansion_mode, "off")
        return None
    client_overrides: dict[str, Any] = {}
    if line_overrides is not None:
        client_overrides = {
            "provider_name": line_overrides[0],
            "base_url": line_overrides[1],
            "api_key": line_overrides[2],
        }
    result = QueryExpander(
        make_llm_client(
            settings,
            connection,
            operation="query_expansion",
            model=settings.query_expansion_model,
            span_recorder=span_recorder,
            runtime=runtime,
            **client_overrides,
        ),
        max_concurrency=settings.query_expansion_max_concurrency,
    )
    _record_component_health("query_expander", settings.query_expansion_mode, settings.query_expansion_mode)
    return result


def make_relation_discoverer(
    settings: Settings,
    connection: sqlite3.Connection | None = None,
    *,
    runtime: ProviderRuntime | None = None,
) -> RelationDiscoveryProtocol | None:
    if settings.relation_discovery_mode == "off":
        _record_component_health("relation_discoverer", settings.relation_discovery_mode, "off")
        return None
    from hl_mem.workers.discover_relations import LLMRelationDiscoverer

    result = LLMRelationDiscoverer(
        make_llm_client(settings, connection, operation="relation_discovery", runtime=runtime)
    )
    _record_component_health(
        "relation_discoverer",
        settings.relation_discovery_mode,
        settings.relation_discovery_mode,
    )
    return result


def make_extractor(
    settings: Settings,
    *,
    require_real: bool = False,
    connection: sqlite3.Connection | None = None,
    runtime: ProviderRuntime | None = None,
) -> ExtractorProtocol:
    if settings.extractor_mode == "fake" and not require_real:
        return FakeExtractor()
    if not settings.llm_api_key:
        raise ConfigurationError("LLM_API_KEY is required")
    structured_mode = (
        StructuredOutputMode.JSON_OBJECT
        if settings.llm_structured_mode == "json_object"
        else StructuredOutputMode.JSON_SCHEMA
    )
    llm_client = make_llm_client(settings, connection, operation="extract", runtime=runtime)
    verifier = (
        EntailmentVerifier(llm_client, structured_mode=structured_mode) if settings.verification_mode != "off" else None
    )
    return LLMExtractor(
        llm_client,
        ChunkingPolicy(
            target_chars=settings.extraction_chunk_target_chars,
            overlap_turns=settings.extraction_chunk_overlap_turns,
            max_split_depth=settings.extraction_max_split_depth,
        ),
        schema_retries=settings.llm_schema_retries,
        structured_mode=structured_mode,
        soft_split_enabled=settings.extraction_soft_split_enabled,
        delta_repair_enabled=settings.extraction_delta_repair_enabled,
        verifier=verifier,
        modes=ExtractionModes(
            verification_mode=settings.verification_mode,
            lesson_signal_mode=settings.lesson_signal_mode,
        ),
    )


def make_extractor_for_type(
    event_type: str,
    settings: Settings,
    *,
    runtime: ProviderRuntime | None = None,
) -> ExtractorProtocol | Literal["explicit"]:
    extractor_name = _EXTRACTOR_REGISTRY.get(event_type, "llm")
    if extractor_name == "explicit":
        return "explicit"
    return make_extractor(settings, runtime=runtime)


__all__ = [
    "ProviderRuntime",
    "component_health",
    "create_provider_runtime",
    "initialize_process",
    "make_embedder",
    "make_extractor",
    "make_extractor_for_type",
    "make_image_describer",
    "make_llm_client",
    "make_provider_registry",
    "make_query_expander",
    "make_relation_discoverer",
    "make_reranker",
]
