"""Mutable counters and diagnostics owned by one extraction run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExtractionRunState:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_call_count: int = 0
    schema_retry_count: int = 0
    repair_count: int = 0
    memorize_decisions: list[tuple[bool, str]] = field(default_factory=list)
    schema_errors: list[dict[str, Any]] = field(default_factory=list)
    secret_rejections: dict[str, int] = field(default_factory=dict)
    relation_metadata_counts: dict[str, int] = field(default_factory=dict)


__all__ = ["ExtractionRunState"]
