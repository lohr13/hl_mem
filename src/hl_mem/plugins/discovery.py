"""白名单优先、元数据优先的 Provider 插件发现。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import metadata
from typing import Any

from hl_mem import __version__
from hl_mem.errors import PluginManifestError
from hl_mem.plugins.contracts import PROVIDER_ENTRY_POINT_GROUP, ProviderPlugin
from hl_mem.plugins.manifest import validate_manifest


def _installed_entry_points() -> tuple[Any, ...]:
    return tuple(metadata.entry_points(group=PROVIDER_ENTRY_POINT_GROUP))


def _distribution_name(entry_point: Any) -> str:
    distribution = getattr(entry_point, "dist", None)
    return str(getattr(distribution, "name", ""))


def discover_plugins(
    enabled: Iterable[str],
    options: Mapping[str, Mapping[str, Any]],
    *,
    entry_points: Iterable[Any] | None = None,
    core_version: str = __version__,
) -> tuple[ProviderPlugin, ...]:
    """只导入显式启用且元数据唯一的 Provider 插件。"""

    enabled_ids = tuple(enabled)
    if len(set(enabled_ids)) != len(enabled_ids):
        raise PluginManifestError("plugins.enabled contains duplicate plugin IDs")
    unknown_options = sorted(set(options) - set(enabled_ids))
    if unknown_options:
        raise PluginManifestError(f"plugin configuration exists for disabled plugin(s): {', '.join(unknown_options)}")

    candidates = tuple(entry_points) if entry_points is not None else _installed_entry_points()
    by_name: dict[str, list[Any]] = {plugin_id: [] for plugin_id in enabled_ids}
    for candidate in candidates:
        candidate_name = str(getattr(candidate, "name", ""))
        if candidate_name in by_name:
            by_name[candidate_name].append(candidate)

    selected: list[Any] = []
    for plugin_id in sorted(by_name):
        matches = sorted(
            by_name[plugin_id],
            key=lambda item: (
                str(getattr(item, "name", "")),
                _distribution_name(item),
                str(getattr(item, "value", "")),
            ),
        )
        if not matches:
            raise PluginManifestError(f"missing provider entry point for enabled plugin {plugin_id!r}")
        if len(matches) != 1:
            distributions = ", ".join(_distribution_name(item) or "<unknown>" for item in matches)
            raise PluginManifestError(
                f"multiple provider entry points found for enabled plugin {plugin_id!r}: {distributions}"
            )
        selected.append(matches[0])

    plugins: list[ProviderPlugin] = []
    for entry_point in selected:
        plugin_id = str(entry_point.name)
        try:
            loaded = entry_point.load()
        except Exception as exc:
            raise PluginManifestError(f"failed to load provider plugin {plugin_id!r}") from exc
        if not callable(loaded):
            raise PluginManifestError(f"provider entry point {plugin_id!r} must load a zero-argument factory")
        try:
            plugin = loaded()
        except Exception as exc:
            raise PluginManifestError(f"provider plugin factory {plugin_id!r} failed") from exc
        if not isinstance(plugin, ProviderPlugin):
            raise PluginManifestError(f"provider plugin factory {plugin_id!r} must return ProviderPlugin")
        if plugin.manifest.id != plugin_id:
            raise PluginManifestError(f"provider entry point {plugin_id!r} returned manifest ID {plugin.manifest.id!r}")
        validate_manifest(plugin.manifest, core_version=core_version)
        plugins.append(plugin)
    return tuple(plugins)


__all__ = ["discover_plugins"]
