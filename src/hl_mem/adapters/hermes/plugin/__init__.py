"""Hermes MemoryProvider 插件入口。

实际实现委托给 :mod:`hl_mem.adapters.hermes.provider`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from hl_mem import __version__, components
from hl_mem.adapters.hermes.discovery import find_hermes_home
from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.config_loader import load_settings
from hl_mem.settings import Settings

logger = logging.getLogger(__name__)


def _resolve_config_paths(
    config_path: str | Path | None,
    env_path: str | Path | None,
) -> tuple[Path, Path]:
    """将插件配置固定到 Hermes Home，同时保留显式路径优先级。"""
    hermes_home = find_hermes_home(None)
    resolved_config_path = (
        Path(config_path).expanduser().resolve() if config_path is not None else hermes_home / "hl_mem.toml"
    )
    resolved_env_path = Path(env_path).expanduser().resolve() if env_path is not None else hermes_home / ".env"
    return resolved_config_path, resolved_env_path


def create_provider(*args: Any, **kwargs: Any) -> HLMemProvider:
    """创建统一的 Hermes 记忆提供器。"""
    settings = kwargs.pop("settings", None)
    if settings is None:
        config_path, env_path = _resolve_config_paths(
            kwargs.pop("config_path", None),
            kwargs.pop("env_path", None),
        )
        settings = load_settings(config_path, env_path)
    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    components.initialize_process(settings)
    return HLMemProvider(*args, settings=settings, **kwargs)


def register(ctx: Any) -> None:
    """向 Hermes 注册 HL-Mem 记忆提供器。"""
    config_path: str | Path = "<unresolved>"
    hermes_home: str | Path = "<unresolved>"
    try:
        config_path, env_path = _resolve_config_paths(None, None)
        hermes_home = config_path.parent
        ctx.register_memory_provider(create_provider(config_path=config_path, env_path=env_path))
    except Exception as error:
        try:
            cwd: str | Path = Path.cwd().resolve()
        except OSError:
            cwd = "<unavailable>"
        logger.exception(
            "Hermes provider registration failed exception_type=%s config_path=%s cwd=%s "
            "hermes_home=%s hl_mem_version=%s",
            type(error).__name__,
            config_path,
            cwd,
            hermes_home,
            __version__,
        )
        raise


__all__ = ["HLMemProvider", "create_provider", "register"]
