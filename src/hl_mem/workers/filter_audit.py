"""Audit projection for Event extraction filter decisions."""

from __future__ import annotations

import time
from typing import Any


def audit_filter_decision(
    audit: Any,
    event: dict[str, Any],
    *,
    allowed: bool,
    reason: str | None,
    started_ns: int,
) -> None:
    """Emit the bounded, content-free filter decision record."""
    audit.emit(
        "filter",
        "evaluated",
        "allow" if allowed else "reject",
        event_id=event["id"],
        duration_us=(time.perf_counter_ns() - started_ns) // 1000,
        detail={
            "reason": reason,
            "event_type": event["event_type"],
            "actor_type": event["actor_type"],
            "content_chars": len(event["content_json"]),
        },
    )
