from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hl_mem.errors import PluginCompatibilityError, PluginManifestError
from hl_mem.plugins.contracts import (
    ProviderCapability,
    ProviderCapabilitySpec,
    ProviderKey,
    ProviderManifest,
    ProviderPlugin,
    ProviderStability,
)
from hl_mem.plugins.discovery import discover_plugins


def _plugin(plugin_id: str = "vendor.plugin") -> ProviderPlugin:
    manifest = ProviderManifest(
        id=plugin_id,
        version="1.0.0",
        api_version=1,
        requires_hl_mem=">=0.36,<2",
        capabilities=(ProviderCapabilitySpec("vendor", ProviderCapability.LLM, ProviderStability.STABLE),),
        config_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    return ProviderPlugin(manifest, {ProviderKey(ProviderCapability.LLM, "vendor"): lambda _context: object()})


@dataclass
class FakeEntryPoint:
    name: str
    plugin: ProviderPlugin | None = None
    value: str = "vendor.module:plugin"
    distribution: str = "vendor-dist"
    fail_on_load: bool = False
    load_calls: int = 0

    @property
    def dist(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.distribution)

    def load(self):  # type: ignore[no-untyped-def]
        self.load_calls += 1
        if self.fail_on_load:
            raise AssertionError("disabled plugin was imported")
        plugin = self.plugin
        return lambda: plugin


def test_disabled_entry_point_is_not_loaded() -> None:
    disabled = FakeEntryPoint("unused.plugin", fail_on_load=True)

    assert discover_plugins((), {}, entry_points=(disabled,)) == ()
    assert disabled.load_calls == 0


def test_enabled_plugins_are_loaded_in_deterministic_id_order() -> None:
    beta = FakeEntryPoint("beta.plugin", _plugin("beta.plugin"))
    alpha = FakeEntryPoint("alpha.plugin", _plugin("alpha.plugin"))

    plugins = discover_plugins(
        ("beta.plugin", "alpha.plugin"),
        {},
        entry_points=(beta, alpha),
        core_version="0.36.1",
    )

    assert tuple(plugin.manifest.id for plugin in plugins) == ("alpha.plugin", "beta.plugin")
    assert alpha.load_calls == beta.load_calls == 1


def test_enabled_plugin_must_have_exactly_one_entry_point() -> None:
    with pytest.raises(PluginManifestError, match="missing.*vendor.plugin"):
        discover_plugins(("vendor.plugin",), {}, entry_points=(), core_version="0.36.1")

    candidates = (
        FakeEntryPoint("vendor.plugin", _plugin(), distribution="first"),
        FakeEntryPoint("vendor.plugin", _plugin(), distribution="second"),
    )
    with pytest.raises(PluginManifestError, match="multiple.*vendor.plugin"):
        discover_plugins(("vendor.plugin",), {}, entry_points=candidates, core_version="0.36.1")
    assert all(candidate.load_calls == 0 for candidate in candidates)


def test_entry_point_and_manifest_ids_must_match() -> None:
    entry_point = FakeEntryPoint("configured.plugin", _plugin("different.plugin"))

    with pytest.raises(PluginManifestError, match="configured.plugin.*different.plugin"):
        discover_plugins(("configured.plugin",), {}, entry_points=(entry_point,), core_version="0.36.1")


def test_plugin_factory_must_return_a_provider_plugin() -> None:
    entry_point = FakeEntryPoint("vendor.plugin", None)

    with pytest.raises(PluginManifestError, match="ProviderPlugin"):
        discover_plugins(("vendor.plugin",), {}, entry_points=(entry_point,), core_version="0.36.1")


def test_configuration_for_disabled_plugin_fails_closed_without_values() -> None:
    with pytest.raises(PluginManifestError, match="unused.plugin") as captured:
        discover_plugins((), {"unused.plugin": {"private": "do-not-log"}}, entry_points=())

    assert "do-not-log" not in str(captured.value)


def test_incompatible_plugin_is_rejected_during_discovery() -> None:
    plugin = _plugin()
    manifest = ProviderManifest(
        id=plugin.manifest.id,
        version=plugin.manifest.version,
        api_version=plugin.manifest.api_version,
        requires_hl_mem=">=2",
        capabilities=plugin.manifest.capabilities,
        config_schema=plugin.manifest.config_schema,
    )
    incompatible = ProviderPlugin(manifest, plugin.factories)

    with pytest.raises(PluginCompatibilityError, match="requires HL-Mem"):
        discover_plugins(
            ("vendor.plugin",),
            {},
            entry_points=(FakeEntryPoint("vendor.plugin", incompatible),),
            core_version="1.0.0",
        )
