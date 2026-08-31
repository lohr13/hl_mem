from __future__ import annotations

from collections.abc import Mapping

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
from hl_mem.plugins.manifest import validate_manifest


def _manifest(
    *,
    plugin_id: str = "example.provider",
    version: str = "1.2.3",
    api_version: int = 1,
    requires_hl_mem: str = ">=0.36.1,<2",
    capabilities: tuple[ProviderCapabilitySpec, ...] | None = None,
    config_schema: Mapping[str, object] | None = None,
) -> ProviderManifest:
    return ProviderManifest(
        id=plugin_id,
        version=version,
        api_version=api_version,
        requires_hl_mem=requires_hl_mem,
        capabilities=capabilities
        or (ProviderCapabilitySpec("example_llm", ProviderCapability.LLM, ProviderStability.STABLE),),
        config_schema=config_schema
        or {
            "type": "object",
            "properties": {"region": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def test_valid_manifest_and_plugin_are_immutable_defensive_records() -> None:
    schema = {
        "type": "object",
        "properties": {"region": {"type": "string"}},
        "additionalProperties": False,
    }
    manifest = _manifest(config_schema=schema)
    plugin = ProviderPlugin(
        manifest,
        {ProviderKey(ProviderCapability.LLM, "example_llm"): lambda _context: object()},
    )
    schema["properties"] = {}

    validate_manifest(manifest, core_version="0.36.1")
    assert "region" in manifest.config_schema["properties"]
    with pytest.raises(TypeError):
        plugin.factories[ProviderKey(ProviderCapability.LLM, "other")] = lambda _context: object()  # type: ignore[index]


@pytest.mark.parametrize(
    ("capability", "stability", "message"),
    (
        (ProviderCapability.IMAGE_DESCRIBER, ProviderStability.STABLE, "image_describer.*experimental"),
        (ProviderCapability.LLM, ProviderStability.EXPERIMENTAL, "llm.*stable"),
        (ProviderCapability.EMBEDDING, ProviderStability.EXPERIMENTAL, "embedding.*stable"),
        (ProviderCapability.RERANKER, ProviderStability.EXPERIMENTAL, "reranker.*stable"),
    ),
)
def test_manifest_cannot_mislabel_capability_stability(
    capability: ProviderCapability,
    stability: ProviderStability,
    message: str,
) -> None:
    manifest = _manifest(capabilities=(ProviderCapabilitySpec("provider", capability, stability),))

    with pytest.raises(PluginManifestError, match=message):
        validate_manifest(manifest, core_version="0.36.1")


@pytest.mark.parametrize("value", ("Bad Plugin", "-leading", "", "x" * 65))
def test_manifest_rejects_invalid_plugin_ids(value: str) -> None:
    with pytest.raises(PluginManifestError, match="plugin ID"):
        validate_manifest(_manifest(plugin_id=value), core_version="0.36.1")


@pytest.mark.parametrize("value", ("release-one", "1..2", ""))
def test_manifest_rejects_invalid_plugin_versions(value: str) -> None:
    with pytest.raises(PluginManifestError, match="plugin version"):
        validate_manifest(_manifest(version=value), core_version="0.36.1")


def test_manifest_rejects_api_major_and_core_version_mismatches() -> None:
    with pytest.raises(PluginCompatibilityError, match="API version 2"):
        validate_manifest(_manifest(api_version=2), core_version="0.36.1")
    with pytest.raises(PluginCompatibilityError, match="requires HL-Mem"):
        validate_manifest(_manifest(requires_hl_mem=">=1.1"), core_version="1.0.0")


def test_manifest_rejects_duplicate_capability_keys() -> None:
    duplicate = ProviderCapabilitySpec("same", ProviderCapability.LLM, ProviderStability.STABLE)
    with pytest.raises(PluginManifestError, match="duplicate capability"):
        validate_manifest(_manifest(capabilities=(duplicate, duplicate)), core_version="0.36.1")


def test_manifest_wraps_invalid_provider_names_as_manifest_errors() -> None:
    capability = ProviderCapabilitySpec("Bad Provider", ProviderCapability.LLM, ProviderStability.STABLE)

    with pytest.raises(PluginManifestError, match="provider name"):
        validate_manifest(_manifest(capabilities=(capability,)), core_version="0.36.1")


@pytest.mark.parametrize(
    ("schema", "message"),
    (
        ({"type": "object", "properties": {}}, "additionalProperties"),
        (
            {
                "type": "object",
                "properties": {"api_key": {"type": "string"}},
                "additionalProperties": False,
            },
            "secret-like",
        ),
        (
            {
                "type": "object",
                "properties": {"nested": {"$ref": "https://schemas.example.test/remote.json"}},
                "additionalProperties": False,
            },
            "remote.*ref",
        ),
        ({"type": "array", "items": {"type": "string"}}, "object schema"),
    ),
)
def test_manifest_rejects_unsafe_or_open_plugin_schemas(schema: dict[str, object], message: str) -> None:
    with pytest.raises(PluginManifestError, match=message):
        validate_manifest(_manifest(config_schema=schema), core_version="0.36.1")


def test_plugin_factories_must_exactly_match_manifest_capabilities() -> None:
    manifest = _manifest()
    with pytest.raises(PluginManifestError, match="factory keys"):
        ProviderPlugin(
            manifest,
            {ProviderKey(ProviderCapability.RERANKER, "other"): lambda _context: object()},
        )
