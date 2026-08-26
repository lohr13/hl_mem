"""ADR-0004 narrow deterministic latest-wins relation for ``config.version``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from hl_mem.domain.claims.state_coordinates import StateCoordinate

TemporalRelation = Literal[
    "duplicate",
    "corroborates",
    "supersedes_existing",
    "historical_predecessor",
    "compatible",
    "needs_review",
]
TEMPORAL_RELATIONS = frozenset(get_args(TemporalRelation))
VersionAliases = Mapping[str, str]
_VERSION_ATOM = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


@dataclass(frozen=True, slots=True)
class VersionClaim:
    claim_id: str
    coordinate: StateCoordinate
    value: str
    event_time: datetime | str | None
    source_authority: int
    evidence_id: str | None
    event_time_trusted: bool
    evidence_grounded: bool
    assertion_kind: str = "observation"
    polarity: str = "positive"
    semantic_anchors: frozenset[tuple[str, str]] = frozenset()
    role_count: int = 1
    payload_count: int = 1
    atomic_value: bool = True


@dataclass(frozen=True, slots=True)
class CurrentnessProof:
    schema_version: str
    producer_contract: str
    package: str
    runtime_version: str
    namespace: str
    canonical_entity_id: str
    alias_version: int
    observed_at: datetime | str | None
    producer_and_owner_verified: bool


@dataclass(frozen=True, slots=True)
class CurrentTipState:
    current_tip_count: int
    old_tip_status: str
    chain_acyclic: bool
    local_snapshot_matches: bool


@dataclass(frozen=True, slots=True)
class TemporalResolution:
    relation: TemporalRelation
    rule_id: str
    coordinate: StateCoordinate
    current_tip_id: str | None
    older_id: str | None
    newer_id: str | None
    event_time_source: str | None
    reason: str


def parse_version_atom(value: str, aliases: VersionAliases | None = None) -> tuple[int, int, int] | None:
    candidate = value if _VERSION_ATOM.fullmatch(value) else (aliases or {}).get(value, value)
    if not isinstance(candidate, str) or (matched := _VERSION_ATOM.fullmatch(candidate)) is None:
        return None
    return tuple(int(part) for part in matched.groups())  # type: ignore[return-value]


def resolve_latest_wins(
    existing: VersionClaim,
    incoming: VersionClaim,
    proof: CurrentnessProof | None,
    tip: CurrentTipState,
    *,
    version_aliases: VersionAliases | None = None,
) -> TemporalResolution:
    if existing.coordinate != incoming.coordinate:
        return _result("compatible", incoming, existing, reason="coordinate_mismatch")
    if incoming.coordinate.canonical_slot != "config.version":
        return _result("compatible", incoming, existing, reason="slot_not_authorized")
    if (reason := _hard_veto(existing, incoming, tip)) is not None:
        return _result("needs_review", incoming, existing, reason=reason)
    if (reason := _proof_veto(incoming, proof, version_aliases)) is not None:
        return _result("needs_review", incoming, existing, reason=reason)
    old_atom = parse_version_atom(existing.value, version_aliases)
    new_atom = parse_version_atom(incoming.value, version_aliases)
    if old_atom is None or new_atom is None:
        return _result("needs_review", incoming, existing, reason="version_atom_unparseable")
    if incoming.source_authority < existing.source_authority:
        return _result("needs_review", incoming, existing, reason="source_authority_downgrade")
    if old_atom == new_atom:
        relation: TemporalRelation = "duplicate" if existing.evidence_id == incoming.evidence_id else "corroborates"
        return _result(relation, incoming, existing, reason="equivalent_version_atom")
    trusted = existing.event_time_trusted and incoming.event_time_trusted
    old_time = _parse_time(existing.event_time) if trusted else None
    new_time = _parse_time(incoming.event_time) if trusted else None
    if old_time is None or new_time is None or old_time == new_time:
        return _result("needs_review", incoming, existing, reason="event_time_missing_or_equal")
    relation = "historical_predecessor" if new_time < old_time else "supersedes_existing"
    if relation == "historical_predecessor":
        older, newer = incoming.claim_id, existing.claim_id
    else:
        older, newer = existing.claim_id, incoming.claim_id
    return _result(relation, incoming, existing, older=older, newer=newer)


def _hard_veto(existing: VersionClaim, incoming: VersionClaim, tip: CurrentTipState) -> str | None:
    if existing.assertion_kind != "observation" or incoming.assertion_kind != "observation":
        return "non_current_context"
    if existing.polarity != "positive" or incoming.polarity != "positive":
        return "polarity_mismatch"
    if existing.semantic_anchors != incoming.semantic_anchors:
        return "critical_anchor_mismatch"
    if incoming.role_count != 1 or incoming.payload_count != 1 or not incoming.atomic_value:
        return "non_atomic_or_multi_payload"
    if any(not item.evidence_id or not item.evidence_grounded for item in (existing, incoming)):
        return "evidence_or_source_unverified"
    if tip.current_tip_count != 1:
        return "current_tip_not_unique"
    if tip.old_tip_status != "active":
        return "tip_disputed_manual_or_open"
    if not tip.chain_acyclic or not tip.local_snapshot_matches:
        return "chain_or_cas_precondition_failed"
    return None


def _proof_veto(incoming: VersionClaim, proof: CurrentnessProof | None, aliases: VersionAliases | None) -> str | None:
    if proof is None:
        return "currentness_proof_missing"
    contract = (proof.schema_version, proof.producer_contract, proof.package)
    if contract != ("status_report_v1", "hl_mem.report-version-v1", "hl_mem"):
        return "currentness_proof_contract_invalid"
    if not proof.producer_and_owner_verified or proof.alias_version < 1 or not proof.canonical_entity_id:
        return "owner_or_source_proof_invalid"
    coordinate = incoming.coordinate
    if proof.namespace != coordinate.namespace or proof.canonical_entity_id != coordinate.canonical_subject:
        return "owner_proof_coordinate_mismatch"
    if parse_version_atom(proof.runtime_version, aliases) != parse_version_atom(incoming.value, aliases):
        return "proof_version_mismatch"
    observed_at = _parse_time(proof.observed_at)
    if observed_at is None or observed_at != _parse_time(incoming.event_time):
        return "proof_event_time_mismatch"
    return None


def _parse_time(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is not None else None


def _result(
    relation: TemporalRelation,
    incoming: VersionClaim,
    existing: VersionClaim,
    reason: str | None = None,
    older: str | None = None,
    newer: str | None = None,
) -> TemporalResolution:
    directional = relation in {"supersedes_existing", "historical_predecessor"}
    return TemporalResolution(
        relation=relation,
        rule_id=f"state-latest-wins-v1:{relation}",
        coordinate=incoming.coordinate,
        current_tip_id=existing.claim_id,
        older_id=older,
        newer_id=newer,
        event_time_source="trusted_event_time" if directional else None,
        reason=reason or "event_time_direction",
    )
