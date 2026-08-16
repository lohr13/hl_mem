"""Hermes memory provider adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .provider import HLMemProvider

__all__ = ["HLMemProvider"]


def __getattr__(name: str) -> Any:
    """延迟导出 provider，保持 discovery 可由纯标准库环境导入。"""
    if name == "HLMemProvider":
        from .provider import HLMemProvider

        return HLMemProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
