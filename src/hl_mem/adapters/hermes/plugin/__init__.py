"""Hermes MemoryProvider 插件入口。

实际实现委托给 :mod:`hl_mem.adapters.hermes.provider`。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _find_hermes_home_for_load_attempt() -> Path:
    environment_home = os.getenv("HERMES_HOME", "").strip()
    if environment_home:
        return Path(environment_home).expanduser().resolve()
    if sys.platform == "win32":
        local_appdata = os.getenv("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return (base / "hermes").resolve()
    return (Path.home() / ".hermes").resolve()


try:
    from hl_mem import __version__ as _load_attempt_version
except Exception:
    _load_attempt_version = "<import-failed>"
try:
    open(_find_hermes_home_for_load_attempt() / "state" / "hl_mem-load.log", "a", encoding="utf-8").write(
        "load-attempt "
        f"timestamp={datetime.now(timezone.utc).isoformat()} "
        f"pid={os.getpid()} hl_mem_version={_load_attempt_version}\n"
    )
except Exception:
    pass

import logging  # noqa: E402
from typing import Any  # noqa: E402

from hl_mem import __version__, components  # noqa: E402
from hl_mem.adapters.hermes.discovery import find_hermes_home  # noqa: E402
from hl_mem.adapters.hermes.provider import HLMemProvider  # noqa: E402
from hl_mem.adapters.hermes.runtime_status import (  # noqa: E402
    RuntimeRegistrationState,
    capture_runtime_identity,
    write_runtime_status,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402

logger = logging.getLogger(__name__)
_RUNTIME_IDENTITY = capture_runtime_identity()


def _record_runtime_status(
    hermes_home: Path | None,
    *,
    status: RuntimeRegistrationState,
    exception_type: str | None = None,
) -> None:
    if hermes_home is None:
        return
    try:
        write_runtime_status(
            hermes_home,
            _RUNTIME_IDENTITY,
            status=status,
            exception_type=exception_type,
        )
    except (OSError, ValueError) as error:
        logger.warning(
            "Unable to record Hermes runtime registration status exception_type=%s",
            type(error).__name__,
        )


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
    hermes_home: Path | None = None
    try:
        config_path, env_path = _resolve_config_paths(None, None)
        hermes_home = config_path.parent
        ctx.register_memory_provider(create_provider(config_path=config_path, env_path=env_path))
        _record_runtime_status(hermes_home, status="registered")
    except Exception as error:
        _record_runtime_status(
            hermes_home,
            status="registration_failed",
            exception_type=type(error).__name__,
        )
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
            hermes_home if hermes_home is not None else "<unresolved>",
            __version__,
        )
        raise


__all__ = ["HLMemProvider", "create_provider", "register"]
