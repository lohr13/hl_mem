"""HL-Mem 内置 Provider 的注册清单。"""

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


def _pending_runtime_factory(context: ProviderFactoryContext) -> object:
    """阻止治理 Runtime 接线完成前误用新 Registry 构造内置调用。"""

    raise ProviderNotFoundError(
        f"built-in {context.key.capability.value} provider {context.key.name!r} is not connected to ProviderRuntime"
    )


def builtin_plugin() -> ProviderPlugin:
    """返回使用公共 Manifest/Factory 记录表达的内置 Provider。"""

    manifest = ProviderManifest(
        id=BUILTIN_PLUGIN_ID,
        version=__version__,
        api_version=PROVIDER_API_VERSION,
        requires_hl_mem=">=0.36.1,<2",
        capabilities=_CAPABILITIES,
        config_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    factories = {spec.key: _pending_runtime_factory for spec in _CAPABILITIES}
    return ProviderPlugin(manifest, factories)


__all__ = ["BUILTIN_PLUGIN_ID", "builtin_plugin"]
