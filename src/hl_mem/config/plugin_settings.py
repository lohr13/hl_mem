"""Configuration and validation owned by the Provider plugin boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from hl_mem.errors import ConfigurationError

_PLUGIN_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_SECRET_OPTION_PARTS = frozenset(
    {"api_key", "authorization", "credential", "credentials", "password", "secret", "token"}
)


def _find_secret_option(value: Mapping[str, Any], prefix: str) -> str | None:
    for key, child in value.items():
        child_path = f"{prefix}.{key}"
        parts = key.casefold().replace("-", "_").split("_")
        if any(part in _SECRET_OPTION_PARTS for part in parts):
            return child_path
        if isinstance(child, Mapping):
            nested = _find_secret_option(child, child_path)
            if nested is not None:
                return nested
    return None


@dataclass(frozen=True)
class PluginsConfig:
    """Configuration owned by the plugins boundary."""

    plugins_enabled: tuple[str, ...] = field(default=(), metadata={"toml": "plugins.enabled"})
    plugin_options: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
        metadata={"plugin_namespace": "plugins"},
    )

    def validate_plugins(self, providers: Iterable[tuple[str, str]]) -> None:
        if len(set(self.plugins_enabled)) != len(self.plugins_enabled):
            raise ConfigurationError("plugins.enabled must not contain duplicate plugin IDs")
        plugin_ids = (*self.plugins_enabled, *self.plugin_options)
        if any(_PLUGIN_ID_PATTERN.fullmatch(plugin_id) is None for plugin_id in plugin_ids):
            raise ConfigurationError("plugins must use lowercase IDs with 1-64 safe characters")
        for plugin_id, options in self.plugin_options.items():
            secret_path = _find_secret_option(options, f"plugins.{plugin_id}")
            if secret_path is not None:
                raise ConfigurationError(f"{secret_path}: plugin options must not contain secrets")
        for provider_path, provider_name in providers:
            if _PLUGIN_ID_PATTERN.fullmatch(provider_name) is None:
                raise ConfigurationError(f"{provider_path} contains an invalid provider name")
