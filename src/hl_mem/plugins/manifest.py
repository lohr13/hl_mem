"""Provider 插件 Manifest 的兼容性与安全校验。"""

from __future__ import annotations

from collections.abc import Mapping
from re import fullmatch, match
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from hl_mem.errors import PluginCompatibilityError, PluginManifestError
from hl_mem.plugins.contracts import (
    PROVIDER_API_VERSION,
    ProviderCapability,
    ProviderManifest,
    ProviderStability,
)

_PLUGIN_ID_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"
_SECRET_PROPERTY_NAMES = {
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _validate_schema_node(node: Any) -> None:
    if isinstance(node, Mapping):
        reference = node.get("$ref")
        if isinstance(reference, str) and (
            match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", reference) or reference.startswith("//")
        ):
            raise PluginManifestError("plugin config schema cannot use a remote ref")
        properties = node.get("properties")
        if isinstance(properties, Mapping):
            for name in properties:
                normalized = str(name).casefold().replace("-", "_")
                if normalized in _SECRET_PROPERTY_NAMES or any(
                    part in _SECRET_PROPERTY_NAMES for part in normalized.split("_")
                ):
                    raise PluginManifestError(f"plugin config schema contains secret-like property {name!r}")
        for child in node.values():
            _validate_schema_node(child)
    elif isinstance(node, (list, tuple)):
        for child in node:
            _validate_schema_node(child)


def validate_manifest(manifest: ProviderManifest, *, core_version: str) -> None:
    """校验插件身份、兼容范围、能力稳定性与非密钥配置 Schema。"""

    if fullmatch(_PLUGIN_ID_PATTERN, manifest.id) is None:
        raise PluginManifestError(f"invalid plugin ID: {manifest.id!r}")
    try:
        Version(manifest.version)
    except InvalidVersion as exc:
        raise PluginManifestError(f"invalid plugin version: {manifest.version!r}") from exc
    if manifest.api_version != PROVIDER_API_VERSION:
        raise PluginCompatibilityError(
            f"plugin {manifest.id!r} uses provider API version {manifest.api_version}; "
            f"host supports {PROVIDER_API_VERSION}"
        )
    try:
        requirement = SpecifierSet(manifest.requires_hl_mem)
    except InvalidSpecifier as exc:
        raise PluginManifestError(f"invalid HL-Mem version requirement: {manifest.requires_hl_mem!r}") from exc
    if Version(core_version) not in requirement:
        raise PluginCompatibilityError(
            f"plugin {manifest.id!r} requires HL-Mem {manifest.requires_hl_mem}; host is {core_version}"
        )

    try:
        keys = [spec.key for spec in manifest.capabilities]
    except ValueError as exc:
        raise PluginManifestError(str(exc)) from exc
    if len(set(keys)) != len(keys):
        raise PluginManifestError("plugin manifest contains a duplicate capability")
    expected_stability = {
        ProviderCapability.LLM: ProviderStability.STABLE,
        ProviderCapability.EMBEDDING: ProviderStability.STABLE,
        ProviderCapability.RERANKER: ProviderStability.STABLE,
        ProviderCapability.IMAGE_DESCRIBER: ProviderStability.EXPERIMENTAL,
    }
    for spec in manifest.capabilities:
        expected = expected_stability[spec.capability]
        if spec.stability is not expected:
            raise PluginManifestError(
                f"{spec.capability.value} capability must be declared {expected.value}, not {spec.stability.value}"
            )

    schema = _thaw(manifest.config_schema)
    if schema.get("type") != "object":
        raise PluginManifestError("plugin config must use an object schema")
    if schema.get("additionalProperties") is not False:
        raise PluginManifestError("plugin config schema must set additionalProperties to false")
    _validate_schema_node(schema)
    try:
        validator_for(schema).check_schema(schema)
    except SchemaError as exc:
        raise PluginManifestError(f"invalid plugin config schema: {exc.message}") from exc


__all__ = ["validate_manifest"]
