"""Deterministic source and session governance for persisted memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence, TypeAlias, cast

OriginClass: TypeAlias = Literal[
    "direct_user",
    "agent",
    "external",
    "external_derived",
    "system",
    "unknown",
]
SessionKind: TypeAlias = Literal["interactive", "cron", "heartbeat", "subagent", "unknown"]
ProvenanceMode: TypeAlias = Literal["observe", "enforce"]

ORIGIN_CLASSES = frozenset({"direct_user", "agent", "external", "external_derived", "system", "unknown"})
SESSION_KINDS = frozenset({"interactive", "cron", "heartbeat", "subagent", "unknown"})
PROVENANCE_MODES = frozenset({"observe", "enforce"})
_ORIGIN_PRIORITY = {
    "direct_user": 0,
    "agent": 1,
    "unknown": 2,
    "external": 3,
    "external_derived": 4,
    "system": 5,
}
_SESSION_PRIORITY = {"interactive": 0, "unknown": 1, "cron": 2, "subagent": 3, "heartbeat": 4}


@dataclass(frozen=True)
class EventProvenance:
    origin_class: OriginClass
    session_kind: SessionKind


@dataclass(frozen=True)
class ProvenanceSummary:
    origin_class: OriginClass
    session_kind: SessionKind
    external: bool
    automated: bool
    explicit_memory: bool


@dataclass(frozen=True)
class ExtractionAdmission:
    allowed: bool
    reason_code: str
    summary: ProvenanceSummary


@dataclass(frozen=True)
class ClaimAdmission:
    allowed: bool
    reason_code: str
    summary: ProvenanceSummary
    source_authority: Literal["low"] | None = None
    assertion_kind: Literal["observation", "inference"] | None = None
    scope: Literal["temporal", "permanent"] | None = None
    preserve_existing: bool = False


def validate_event_provenance(event: Mapping[str, object]) -> EventProvenance:
    """Validate one Event's closed provenance contract with legacy defaults."""
    origin = event.get("origin_class", "unknown")
    session = event.get("session_kind", "unknown")
    if not isinstance(origin, str) or origin not in ORIGIN_CLASSES:
        raise ValueError("origin_class must be a supported provenance value")
    if not isinstance(session, str) or session not in SESSION_KINDS:
        raise ValueError("session_kind must be a supported provenance value")
    return EventProvenance(cast(OriginClass, origin), cast(SessionKind, session))


def _validate_mode(mode: str) -> ProvenanceMode:
    if mode not in PROVENANCE_MODES:
        raise ValueError("provenance mode must be 'observe' or 'enforce'")
    return cast(ProvenanceMode, mode)


def aggregate_event_provenance(events: Sequence[Mapping[str, object]]) -> ProvenanceSummary:
    """Aggregate Evidence sources using the most conservative deterministic labels."""
    if not events:
        events = ({},)
    validated = [validate_event_provenance(event) for event in events]
    origin = max((item.origin_class for item in validated), key=_ORIGIN_PRIORITY.__getitem__)
    session = max((item.session_kind for item in validated), key=_SESSION_PRIORITY.__getitem__)
    return ProvenanceSummary(
        origin_class=origin,
        session_kind=session,
        external=any(item.origin_class in {"external", "external_derived"} for item in validated),
        automated=session in {"cron", "heartbeat", "subagent"} or origin == "system",
        explicit_memory=any(event.get("event_type") == "explicit_memory" for event in events),
    )


def decide_extraction_admission(
    events: Sequence[Mapping[str, object]],
    *,
    mode: ProvenanceMode,
) -> ExtractionAdmission:
    """Gate paid extraction for automated sessions without guessing source truth."""
    _validate_mode(mode)
    summary = aggregate_event_provenance(events)
    blocked_session = summary.session_kind if summary.session_kind in {"heartbeat", "subagent"} else None
    if blocked_session is None:
        return ExtractionAdmission(True, "allowed", summary)
    if mode == "observe":
        return ExtractionAdmission(True, f"observe_{blocked_session}", summary)
    return ExtractionAdmission(False, f"blocked_{blocked_session}", summary)


def decide_claim_admission(
    events: Sequence[Mapping[str, object]],
    *,
    assertion_kind: str,
    scope: str,
    mode: ProvenanceMode,
) -> ClaimAdmission:
    """Return bounded Claim overrides while preserving direct and legacy semantics."""
    extraction = decide_extraction_admission(events, mode=mode)
    summary = extraction.summary
    if not extraction.allowed:
        return ClaimAdmission(False, extraction.reason_code, summary)
    if mode == "observe" or (summary.origin_class == "unknown" and summary.session_kind == "unknown"):
        return ClaimAdmission(True, extraction.reason_code, summary, preserve_existing=True)
    restricted = summary.external or summary.origin_class == "system" or summary.session_kind == "cron"
    if not restricted:
        return ClaimAdmission(True, extraction.reason_code, summary, preserve_existing=True)
    governed_kind: Literal["observation", "inference"] = "inference" if assertion_kind == "inference" else "observation"
    governed_scope: Literal["temporal", "permanent"] = "permanent" if summary.explicit_memory else "temporal"
    return ClaimAdmission(
        True,
        "restricted_source",
        summary,
        source_authority="low",
        assertion_kind=governed_kind,
        scope=governed_scope,
    )
