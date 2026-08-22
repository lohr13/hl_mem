"""Offline evaluation tools for public long-memory benchmarks."""

from __future__ import annotations

from typing import Any

__all__ = ["BenchmarkRunner", "LongMemEvalAdapter"]


def __getattr__(name: str) -> Any:
    """Lazy-load heavy dependencies so the runtime guard runs first."""

    if name == "BenchmarkRunner":
        from hl_mem.evaluation.runner import BenchmarkRunner

        return BenchmarkRunner
    if name == "LongMemEvalAdapter":
        from hl_mem.evaluation.longmemeval import LongMemEvalAdapter

        return LongMemEvalAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
