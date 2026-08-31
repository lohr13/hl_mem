from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from hl_mem.errors import ConfigurationError, PluginConflictError, PluginManifestError, ProviderNotFoundError
from hl_mem.plugins.contracts import (
    ProviderCapability,
    ProviderCapabilitySpec,
    ProviderKey,
    ProviderManifest,
    ProviderPlugin,
    ProviderStability,
)
from hl_mem.plugins.registry import ProviderRegistry, build_provider_registry
from hl_mem.settings import Settings


def _plugin(
    plugin_id: str,
    capability: ProviderCapability,
    name: str,
    *,
    schema: dict[str, object] | None = None,
    result: object | None = None,
) -> ProviderPlugin:
    stability = (
        ProviderStability.EXPERIMENTAL if capability is ProviderCapability.IMAGE_DESCRIBER else ProviderStability.STABLE
    )
    manifest = ProviderManifest(
        id=plugin_id,
        version="1.0.0",
        api_version=1,
        requires_hl_mem=">=0.36,<2",
        capabilities=(ProviderCapabilitySpec(name, capability, stability),),
        config_schema=schema or {"type": "object", "properties": {}, "additionalProperties": False},
    )
    key = ProviderKey(capability, name)
    return ProviderPlugin(manifest, {key: lambda _context: result if result is not None else object()})


class FakeEntryPoint:
    value = "vendor.module:plugin"
    dist = SimpleNamespace(name="vendor-dist")

    def __init__(self, plugin: ProviderPlugin) -> None:
        self.name = plugin.manifest.id
        self.plugin = plugin

    def load(self):  # type: ignore[no-untyped-def]
        return lambda: self.plugin


def _settings(plugin: ProviderPlugin, options: dict[str, object] | None = None) -> Settings:
    return replace(
        Settings.for_test(),
        plugins_enabled=(plugin.manifest.id,),
        plugin_options=MappingProxyType(
            {plugin.manifest.id: MappingProxyType(options or {})},
        ),
    )


def test_registry_keys_are_capability_qualified_and_deterministic() -> None:
    registry = ProviderRegistry()
    llm = _plugin("llm.plugin", ProviderCapability.LLM, "shared")
    reranker = _plugin("rerank.plugin", ProviderCapability.RERANKER, "shared")

    registry.register(reranker)
    registry.register(llm)
    registry.freeze()

    assert registry.keys() == (
        ProviderKey(ProviderCapability.LLM, "shared"),
        ProviderKey(ProviderCapability.RERANKER, "shared"),
    )


def test_registry_rejects_collisions_and_registration_after_freeze() -> None:
    registry = ProviderRegistry()
    first = _plugin("first.plugin", ProviderCapability.LLM, "shared")
    second = _plugin("second.plugin", ProviderCapability.LLM, "shared")
    registry.register(first)

    with pytest.raises(PluginConflictError, match=r"first\.plugin.*second\.plugin"):
        registry.register(second)

    registry.freeze()
    with pytest.raises(PluginConflictError, match="frozen"):
        registry.register(_plugin("third.plugin", ProviderCapability.LLM, "other"))


def test_builtin_collision_fails_before_registry_is_returned() -> None:
    external = _plugin("vendor.plugin", ProviderCapability.LLM, "dashscope")

    with pytest.raises(PluginConflictError, match=r"hl-mem\.builtin.*vendor\.plugin"):
        build_provider_registry(_settings(external), entry_points=(FakeEntryPoint(external),))


def test_builtin_registry_exposes_current_provider_names() -> None:
    registry = build_provider_registry(Settings.for_test(), entry_points=())

    assert registry.keys() == (
        ProviderKey(ProviderCapability.EMBEDDING, "dashscope"),
        ProviderKey(ProviderCapability.IMAGE_DESCRIBER, "dashscope"),
        ProviderKey(ProviderCapability.LLM, "dashscope"),
        ProviderKey(ProviderCapability.LLM, "openai_compatible"),
        ProviderKey(ProviderCapability.LLM, "zhipu"),
        ProviderKey(ProviderCapability.RERANKER, "dashscope"),
    )


def test_registry_validates_plugin_options_before_freezing() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"region": {"type": "string"}},
        "required": ["region"],
        "additionalProperties": False,
    }
    plugin = _plugin("vendor.plugin", ProviderCapability.LLM, "vendor", schema=schema)

    with pytest.raises(PluginManifestError, match=r"plugins\.vendor\.plugin.*region") as captured:
        build_provider_registry(
            _settings(plugin, {"region": 42, "ignored": "do-not-log"}),
            entry_points=(FakeEntryPoint(plugin),),
        )

    assert "do-not-log" not in str(captured.value)


def test_registry_constructs_with_immutable_scoped_options() -> None:
    seen: list[object] = []
    manifest = ProviderManifest(
        id="vendor.plugin",
        version="1.0.0",
        api_version=1,
        requires_hl_mem=">=0.36,<2",
        capabilities=(ProviderCapabilitySpec("vendor", ProviderCapability.LLM, ProviderStability.STABLE),),
        config_schema={
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    key = ProviderKey(ProviderCapability.LLM, "vendor")
    marker = object()

    def factory(context):  # type: ignore[no-untyped-def]
        seen.append(context)
        return marker

    plugin = ProviderPlugin(manifest, {key: factory})
    registry = build_provider_registry(
        _settings(plugin, {"region": "cn"}),
        entry_points=(FakeEntryPoint(plugin),),
    )

    assert registry.create(key, {"model": "one"}) is marker
    context = seen[0]
    assert context.plugin_options == {"region": "cn"}  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        context.plugin_options["region"] = "us"  # type: ignore[attr-defined,index]


def test_typed_registry_construction_rejects_wrong_adapter_shape() -> None:
    plugin = _plugin("vendor.plugin", ProviderCapability.LLM, "vendor")
    registry = build_provider_registry(_settings(plugin), entry_points=(FakeEntryPoint(plugin),))

    with pytest.raises(PluginManifestError, match="LLMProviderAdapter"):
        registry.create_llm("vendor", {})


def test_registry_rejects_unknown_provider_key() -> None:
    registry = ProviderRegistry()
    registry.freeze()

    with pytest.raises(ProviderNotFoundError, match="missing"):
        registry.create(ProviderKey(ProviderCapability.LLM, "missing"), {})


def test_registry_rejects_configured_provider_without_matching_capability() -> None:
    settings = replace(Settings.for_test(), llm_provider="missing")

    with pytest.raises(ProviderNotFoundError, match=r"llm\.provider.*missing"):
        build_provider_registry(settings, entry_points=())


def test_direct_settings_cannot_bypass_plugin_secret_rejection() -> None:
    settings = replace(
        Settings.for_test(),
        plugins_enabled=("vendor.plugin",),
        plugin_options=MappingProxyType(
            {"vendor.plugin": MappingProxyType({"nested": MappingProxyType({"api_token": "do-not-log"})})}
        ),
    )

    with pytest.raises(ConfigurationError, match=r"plugins\.vendor\.plugin\.nested\.api_token") as captured:
        build_provider_registry(settings, entry_points=())

    assert "do-not-log" not in str(captured.value)
