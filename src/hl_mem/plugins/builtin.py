"""Built-in Providers expressed through the public plugin manifest."""

from __future__ import annotations

from hl_mem import __version__
from hl_mem.errors import ProviderNotFoundError
from hl_mem.plugins.contracts import (
    PROVIDER_API_VERSION,
    ProviderCapability,
    ProviderCapabilitySpec,
    ProviderFactoryContext,
    ProviderManifest,
    ProviderPlugin,
    ProviderStability,
)

BUILTIN_PLUGIN_ID = "hl-mem.builtin"

_CAPABILITIES = (
    ProviderCapabilitySpec("dashscope", ProviderCapability.LLM, ProviderStability.STABLE),
    ProviderCapabilitySpec("zhipu", ProviderCapability.LLM, ProviderStability.STABLE),
    ProviderCapabilitySpec("openai_compatible", ProviderCapability.LLM, ProviderStability.STABLE),
    ProviderCapabilitySpec("dashscope", ProviderCapability.EMBEDDING, ProviderStability.STABLE),
    ProviderCapabilitySpec("dashscope", ProviderCapability.RERANKER, ProviderStability.STABLE),
    ProviderCapabilitySpec(
        "dashscope",
        ProviderCapability.IMAGE_DESCRIBER,
        ProviderStability.EXPERIMENTAL,
    ),
)


def _builtin_runtime_factory(context: ProviderFactoryContext) -> object:
    """Create only capabilities already migrated onto ProviderRuntime."""
    if context.key.capability is ProviderCapability.LLM:
        from hl_mem.llm.providers import make_builtin_llm_provider

        return make_builtin_llm_provider(context)
    if context.key.capability is ProviderCapability.EMBEDDING:
        from hl_mem.ingest.embedder import make_builtin_embedding_provider

        return make_builtin_embedding_provider(context)
    raise ProviderNotFoundError(
        f"built-in {context.key.capability.value} provider {context.key.name!r} is not connected to ProviderRuntime"
    )


def builtin_plugin() -> ProviderPlugin:
    manifest = ProviderManifest(
        id=BUILTIN_PLUGIN_ID,
        version=__version__,
        api_version=PROVIDER_API_VERSION,
        requires_hl_mem=">=0.36.1,<2",
        capabilities=_CAPABILITIES,
        config_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    factories = {spec.key: _builtin_runtime_factory for spec in _CAPABILITIES}
    return ProviderPlugin(manifest, factories)


__all__ = ["BUILTIN_PLUGIN_ID", "builtin_plugin"]
