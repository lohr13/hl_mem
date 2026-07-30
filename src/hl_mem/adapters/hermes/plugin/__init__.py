"""Hermes MemoryProvider 插件入口。

实际实现委托给 :mod:`hl_mem.adapters.hermes.provider`。
"""

from __future__ import annotations

from typing import Any

from hl_mem import components
from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.config_loader import load_settings
from hl_mem.settings import Settings


def create_provider(*args: Any, **kwargs: Any) -> HLMemProvider:
    """创建统一的 Hermes 记忆提供器。"""
    settings = kwargs.pop("settings", None)
    if settings is None:
        settings = load_settings(
            kwargs.pop("config_path", None),
            kwargs.pop("env_path", None),
        )
    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    components.initialize_process(settings)
    return HLMemProvider(*args, settings=settings, **kwargs)


def register(ctx: Any) -> None:
    """向 Hermes 注册 HL-Mem 记忆提供器。"""
    ctx.register_memory_provider(create_provider())


__all__ = ["HLMemProvider", "create_provider", "register"]
