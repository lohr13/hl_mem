"""Worker-facing provenance gate with bounded audit output."""

from __future__ import annotations

from typing import Any, Sequence

from hl_mem.domain.provenance import ProvenanceMode, decide_extraction_admission


def provenance_extraction_rejection(
    audit: Any,
    events: Sequence[dict[str, Any]],
    *,
    mode: ProvenanceMode,
) -> dict[str, Any] | None:
    """Audit admission and return the standard no-call result when blocked."""
    decision = decide_extraction_admission(events, mode=mode)
    audit.emit(
        "provenance",
        "extraction_admission",
        "allow" if decision.allowed else "reject",
        event_id=events[0]["id"],
        detail={
            "reason": decision.reason_code,
            "origin_class": decision.summary.origin_class,
            "session_kind": decision.summary.session_kind,
            "event_count": len(events),
        },
    )
    if decision.allowed:
        return None
    return {
        "events": len(events),
        "eligible_events": 0,
        "claims": 0,
        "stored": 0,
        "skipped": 0,
        "rejections": [{"reason": decision.reason_code}],
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
