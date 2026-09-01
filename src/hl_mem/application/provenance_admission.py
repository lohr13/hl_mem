"""Application projection of pure provenance policy onto extracted Claims."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any, Sequence

from hl_mem.domain.provenance import ClaimAdmission, decide_claim_admission
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings


@dataclass(frozen=True)
class GovernedClaimInput:
    extracted: ExtractedClaim
    authority: str | None
    admission: ClaimAdmission
    evidence_count: int


def govern_claim_input(
    connection: sqlite3.Connection,
    extracted: ExtractedClaim,
    authority: str | None,
    evidence_events: Sequence[dict[str, Any]],
) -> GovernedClaimInput:
    """Apply deterministic overrides without constructing or embedding a Claim."""
    settings = getattr(connection, "hl_mem_settings", None) or Settings()
    admission = decide_claim_admission(
        evidence_events,
        assertion_kind=extracted.assertion_kind,
        scope=extracted.scope,
        mode=settings.provenance_mode,
    )
    governed_extracted = extracted
    if admission.assertion_kind is not None or admission.scope is not None:
        governed_extracted = replace(
            extracted,
            assertion_kind=admission.assertion_kind or extracted.assertion_kind,
            scope=admission.scope or extracted.scope,
        )
    return GovernedClaimInput(
        governed_extracted,
        admission.source_authority or authority,
        admission,
        len(evidence_events),
    )


def provenance_audit_event(
    event: dict[str, Any],
    governed: GovernedClaimInput,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Render only bounded reason codes and labels for deferred audit flush."""
    admission = governed.admission
    return (
        ("provenance", "claim_admission", "allow" if admission.allowed else "reject"),
        {
            "event_id": event.get("id"),
            "detail": {
                "reason": admission.reason_code,
                "origin_class": admission.summary.origin_class,
                "session_kind": admission.summary.session_kind,
                "evidence_count": governed.evidence_count,
            },
        },
    )
