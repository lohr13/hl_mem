"""Hermes MemoryProvider 插件入口。

实际实现委托给 :mod:`hl_mem.adapters.hermes.provider`。
"""

from __future__ import annotations

from typing import Any

from hl_mem.adapters.hermes.provider import HLMemProvider


def create_provider(*args: Any, **kwargs: Any) -> HLMemProvider:
    """创建统一的 Hermes 记忆提供器。"""
    return HLMemProvider(*args, **kwargs)


def register(ctx: Any) -> None:
    """向 Hermes 注册 HL-Mem 记忆提供器。"""
    ctx.register_memory_provider(create_provider())


__all__ = ["HLMemProvider", "create_provider", "register"]
