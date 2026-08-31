"""冲突安全、冻结后只读的 Provider Registry。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for

from hl_mem import __version__
from hl_mem.config.models import Settings
from hl_mem.errors import PluginConflictError, PluginManifestError, ProviderNotFoundError
from hl_mem.plugins.builtin import builtin_plugin
from hl_mem.plugins.contracts import (
    EmbeddingProviderAdapter,
    ImageProviderAdapter,
    LLMProviderAdapter,
    ProviderCapability,
    ProviderFactory,
    ProviderFactoryContext,
    ProviderKey,
    ProviderPlugin,
    RerankerProviderAdapter,
)
from hl_mem.plugins.discovery import discover_plugins
from hl_mem.plugins.manifest import validate_manifest


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class _Registration:
    plugin_id: str
    factory: ProviderFactory
    plugin_options: Mapping[str, Any]
    builtin: bool


class ProviderRegistry:
    """按能力与名称索引 Provider，并在组装完成后冻结。"""

    def __init__(self) -> None:
        self._registrations: dict[ProviderKey, _Registration] = {}
        self._frozen = False

    def register(
        self,
        plugin: ProviderPlugin,
        *,
        builtin: bool = False,
        plugin_options: Mapping[str, Any] | None = None,
    ) -> None:
        if self._frozen:
            raise PluginConflictError("provider registry is frozen")
        options = _freeze(plugin_options or {})
        collisions = sorted(
            ((key, self._registrations[key].plugin_id) for key in plugin.factories if key in self._registrations),
            key=lambda item: (item[0].capability.value, item[0].name),
        )
        if collisions:
            key, existing_id = collisions[0]
            raise PluginConflictError(
                f"provider capability {key.capability.value}:{key.name} is declared by both "
                f"{existing_id!r} and {plugin.manifest.id!r}"
            )
        for key, factory in plugin.factories.items():
            self._registrations[key] = _Registration(plugin.manifest.id, factory, options, builtin)

    def freeze(self) -> None:
        self._frozen = True

    def keys(self) -> tuple[ProviderKey, ...]:
        return tuple(sorted(self._registrations, key=lambda key: (key.capability.value, key.name)))

    def plugin_id_for(self, key: ProviderKey) -> str:
        """Return the immutable owner identity used by host governance."""
        if not self._frozen:
            raise PluginConflictError("provider registry must be frozen before resolving providers")
        registration = self._registrations.get(key)
        if registration is None:
            raise ProviderNotFoundError(f"provider {key.capability.value}:{key.name} is missing")
        return registration.plugin_id

    def create(self, key: ProviderKey, core_options: Mapping[str, Any]) -> object:
        if not self._frozen:
            raise PluginConflictError("provider registry must be frozen before creating providers")
        registration = self._registrations.get(key)
        if registration is None:
            raise ProviderNotFoundError(f"provider {key.capability.value}:{key.name} is missing")
        context = ProviderFactoryContext(key, core_options, registration.plugin_options)
        return registration.factory(context)

    def create_llm(self, name: str, core_options: Mapping[str, Any]) -> LLMProviderAdapter:
        adapter = self.create(ProviderKey(ProviderCapability.LLM, name), core_options)
        if not isinstance(adapter, LLMProviderAdapter):
            raise PluginManifestError(f"LLM provider {name!r} factory did not return LLMProviderAdapter")
        return adapter

    def create_embedding(self, name: str, core_options: Mapping[str, Any]) -> EmbeddingProviderAdapter:
        adapter = self.create(ProviderKey(ProviderCapability.EMBEDDING, name), core_options)
        if not isinstance(adapter, EmbeddingProviderAdapter):
            raise PluginManifestError(f"Embedding provider {name!r} factory did not return EmbeddingProviderAdapter")
        return adapter

    def create_reranker(self, name: str, core_options: Mapping[str, Any]) -> RerankerProviderAdapter:
        adapter = self.create(ProviderKey(ProviderCapability.RERANKER, name), core_options)
        if not isinstance(adapter, RerankerProviderAdapter):
            raise PluginManifestError(f"Reranker provider {name!r} factory did not return RerankerProviderAdapter")
        return adapter

    def create_image_describer(self, name: str, core_options: Mapping[str, Any]) -> ImageProviderAdapter:
        adapter = self.create(ProviderKey(ProviderCapability.IMAGE_DESCRIBER, name), core_options)
        if not isinstance(adapter, ImageProviderAdapter):
            raise PluginManifestError(f"Image provider {name!r} factory did not return ImageProviderAdapter")
        return adapter


def _validate_options(plugin: ProviderPlugin, options: Mapping[str, Any]) -> None:
    schema = _thaw(plugin.manifest.config_schema)
    instance = _thaw(options)
    validator = validator_for(schema)(schema)
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda item: (-len(item.path), tuple(str(part) for part in item.path), item.validator),
    )
    if not errors:
        return
    error: JsonSchemaValidationError = errors[0]
    suffix = ".".join(str(part) for part in error.path)
    option_path = f"plugins.{plugin.manifest.id}" + (f".{suffix}" if suffix else "")
    raise PluginManifestError(f"{option_path}: plugin configuration does not match its schema")


def build_provider_registry(
    settings: Settings,
    *,
    entry_points: Iterable[Any] | None = None,
    core_version: str = __version__,
) -> ProviderRegistry:
    """校验完整插件集合并返回冻结 Registry。"""

    settings.validate()
    builtin = builtin_plugin()
    validate_manifest(builtin.manifest, core_version=core_version)
    external = discover_plugins(
        settings.plugins_enabled,
        settings.plugin_options,
        entry_points=entry_points,
        core_version=core_version,
    )

    registry = ProviderRegistry()
    registry.register(builtin, builtin=True)
    for plugin in external:
        options = settings.plugin_options.get(plugin.manifest.id, {})
        _validate_options(plugin, options)
        registry.register(plugin, plugin_options=options)
    selections = [
        ("llm.provider", ProviderKey(ProviderCapability.LLM, settings.llm_provider)),
        ("embedding.provider", ProviderKey(ProviderCapability.EMBEDDING, settings.embedding_provider)),
        ("reranker.provider", ProviderKey(ProviderCapability.RERANKER, settings.reranker_provider)),
        (
            "image_describer.provider",
            ProviderKey(ProviderCapability.IMAGE_DESCRIBER, settings.image_describer_provider),
        ),
    ]
    if settings.query_expansion_provider is not None:
        selections.append(
            (
                "recall.query_expansion_provider",
                ProviderKey(ProviderCapability.LLM, settings.query_expansion_provider),
            )
        )
    available = set(registry.keys())
    for setting_path, key in selections:
        if key not in available:
            raise ProviderNotFoundError(
                f"{setting_path} references unregistered provider {key.capability.value}:{key.name}"
            )
    registry.freeze()
    return registry


__all__ = ["ProviderRegistry", "build_provider_registry"]
