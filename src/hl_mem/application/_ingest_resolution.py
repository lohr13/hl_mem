"""Conflict and temporal resolution primitives for the ingest transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.claims.conflicts import ConflictResolver, compute_claim_pair_key
from hl_mem.domain.claims.dedup import (
    DEDUP_EMBEDDING_TEXT_VERSION,
    DEDUP_POLICY_VERSION,
    Deduplicator,
    compute_dedup_pair_key,
)
from hl_mem.domain.claims.state_projection import state_candidate_key
from hl_mem.domain.claims.state_transitions import evaluate_state_or_temporal_link as evaluate_temporal_link
from hl_mem.errors import ConflictError
from hl_mem.lifecycle import assert_transition
from hl_mem.monitoring.metrics import AdmissionMetrics
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository


@dataclass(frozen=True)
class _ConflictGroupResolution:
    outcome: str
    representative: dict[str, Any]
    member_outcomes: tuple[tuple[str, str], ...]
    active_count: int


@dataclass(frozen=True)
class _TemporalResolution:
    outcome: str
    representative: dict[str, Any]
    members: tuple[dict[str, Any], ...]
    member_outcomes: tuple[tuple[str, str, str | None, str], ...]
    rationale: str
    snapshot_order: str | None


def _find_resolution(
    claims: ClaimRepository, claim: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Find an exact duplicate or mutually-exclusive conflict candidates."""
    exact = claims.find_by_fact_hash(claim["namespace_key"], claim["fact_hash"])
    if exact is not None:
        if Deduplicator._deterministic_check(exact, claim) == "equivalent":
            return exact, []
        return None, []
    conflict_key = claim.get("conflict_key")
    exclusive = is_mutually_exclusive_attribute(claim.get("canonical_slot")) and not state_candidate_key(claim)
    existing = claims.find_by_conflict_key(conflict_key) if conflict_key and exclusive else []
    return None, existing


def _resolve_conflict_group(
    members: Sequence[dict[str, Any]],
    new_claim: dict[str, Any],
    *,
    preferred_id: str | None = None,
) -> _ConflictGroupResolution:
    if not members:
        raise ValueError("conflict group must contain at least one member")
    resolver = ConflictResolver()
    member_outcomes = tuple((member["id"], resolver.resolve(member, new_claim)) for member in members)
    active_members = [member for member in members if member.get("status") == "active"]
    unique_outcomes = {outcome for _, outcome in member_outcomes}
    outcome = next(iter(unique_outcomes)) if len(unique_outcomes) == 1 else "uncertain"
    if len(active_members) > 1:
        outcome = "uncertain"
    representative = active_members[0] if active_members else members[0]
    if not active_members and preferred_id is not None:
        representative = next((member for member in members if member["id"] == preferred_id), representative)
    return _ConflictGroupResolution(
        outcome=outcome,
        representative=representative,
        member_outcomes=member_outcomes,
        active_count=len(active_members),
    )


def _resolve_temporal_candidates(
    members: Sequence[dict[str, Any]], new_claim: dict[str, Any]
) -> _TemporalResolution | None:
    evaluated = [(member, evaluate_temporal_link(member, new_claim)) for member in members]
    actionable = [(member, decision) for member, decision in evaluated if decision.outcome != "not_applicable"]
    if not actionable:
        return None
    competing = [(member, decision) for member, decision in actionable if decision.outcome != "distinct_series"]
    selected = competing or actionable
    outcomes = {decision.outcome for _, decision in selected}
    outcome = next(iter(outcomes)) if len(outcomes) == 1 else "uncertain"
    snapshot_orders = {decision.snapshot_order for _, decision in selected if decision.outcome == "snapshot_advance"}
    mixed_snapshot_order = outcome == "snapshot_advance" and len(snapshot_orders) != 1
    if mixed_snapshot_order:
        outcome = "uncertain"
    representative, representative_decision = selected[0]
    rationale = ("temporal_member_outcomes_mixed", representative_decision.rationale)[len(outcomes) == 1]
    if mixed_snapshot_order:
        rationale = "snapshot_order_mixed"
    return _TemporalResolution(
        outcome=outcome,
        representative=representative,
        members=tuple(member for member, _ in selected),
        member_outcomes=tuple(
            (str(member["id"]), decision.outcome, decision.rule_id, decision.rationale)
            for member, decision in actionable
        ),
        rationale=rationale,
        snapshot_order=(next(iter(snapshot_orders)) if outcome == "snapshot_advance" else None),
    )


def _converge_entailed_group(
    claims: ClaimRepository,
    resolution: _ConflictGroupResolution,
    now: str,
) -> str:
    """Converge an entailed group on one active representative."""
    if resolution.outcome != "entails":
        raise ValueError("only an entailed conflict group can be converged")
    winner = resolution.representative
    members = claims.find_by_conflict_key(winner.get("conflict_key"))
    for member in members:
        if member["id"] == winner["id"]:
            continue
        superseded = claims.supersede_with_inline(
            member["id"],
            winner["id"],
            winner.get("value"),
            now,
            now,
            commit=False,
        )
        if not superseded.applied:
            raise ConflictError(f"entailed conflict group member changed during ingest: {member['id']}")
    if winner.get("status") != "active":
        assert_transition(str(winner.get("status")), "active")
        if not claims.update_status(winner["id"], "active", commit=False):
            raise ConflictError(f"entailed conflict group winner disappeared during ingest: {winner['id']}")
    return str(winner["id"])


def _quarantine_conflict_group(
    claims: ClaimRepository,
    members: Sequence[dict[str, Any]],
    now: str,
    *,
    decision: str,
    rationale: str,
) -> None:
    """Quarantine one mutually-exclusive group under a single case."""
    unique_members = list({str(member["id"]): member for member in members}.values())
    namespaces = {str(member.get("namespace_key") or "") for member in unique_members}
    conflict_keys = {str(member.get("conflict_key") or "") for member in unique_members}
    slots = {str(member.get("canonical_slot") or "") for member in unique_members}
    if (
        len(namespaces) != 1
        or len(conflict_keys) != 1
        or len(slots) != 1
        or not all(is_mutually_exclusive_attribute(slot) for slot in slots)
    ):
        raise ConflictError("group quarantine requires one mutually-exclusive namespace, key, and slot")
    for member in unique_members:
        if member.get("status") in {"active", "candidate"}:
            assert_transition(str(member["status"]), "disputed")
            if not claims.update_status(member["id"], "disputed", commit=False):
                raise ConflictError(f"conflict group member changed during quarantine: {member['id']}")
            member["status"] = "disputed"
    claims.ensure_group_conflict_case(
        unique_members,
        created_at=now,
        decision=decision,
        rationale=rationale,
        commit=False,
    )


def _quarantine_temporal_pair(
    claims: ClaimRepository,
    existing: dict[str, Any],
    new_claim: dict[str, Any],
    now: str,
    *,
    rationale: str,
    id_factory: Callable[[], str],
) -> None:
    """Route one non-operational gray update through the pair-case pipeline."""
    if existing.get("status") != "active" or new_claim.get("status") != "disputed":
        raise ConflictError("temporal pair quarantine requires one active and one disputed claim")
    assert_transition("active", "disputed")
    if not claims.update_status(str(existing["id"]), "disputed", commit=False):
        raise ConflictError(f"temporal candidate changed during quarantine: {existing['id']}")
    claims.ensure_manual_conflict_case(
        {
            "id": id_factory(),
            "pair_key": compute_claim_pair_key(str(existing["id"]), str(new_claim["id"])),
            "left_claim_id": existing["id"],
            "right_claim_id": new_claim["id"],
            "status": "manual_required",
            "decision": "uncertain",
            "rationale": rationale,
            "created_at": now,
        },
        commit=False,
    )


def _persist_resolution(claims: ClaimRepository, claim: dict[str, Any]) -> bool:
    """Write a resolved claim within the transaction owned by the caller."""
    return claims.insert_claim(claim, commit=False)


def _insert_pending_dedup_pair_row(
    connection: sqlite3.Connection,
    existing_claim_id: str,
    new_claim: dict[str, Any],
    similarity: float,
    created_at: str,
    *,
    metrics: AdmissionMetrics,
    id_factory: Callable[[], str],
    settings_factory: Callable[[], Settings],
) -> bool:
    """Record an LLM gray-area pair without a remote call in the write transaction."""
    settings = getattr(connection, "hl_mem_settings", None) or settings_factory()
    pending_count = int(connection.execute("SELECT count(*) FROM dedup_pairs WHERE decision IS NULL").fetchone()[0])
    if pending_count >= settings.dedup_max_pending_pairs:
        metrics.record_dedup_pending_pair_skipped()
        return False
    cursor = connection.execute(
        "INSERT OR IGNORE INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,new_claim_id,pair_source,namespace_key,similarity,"
        "embedding_text_version,policy_version,predicate,created_at"
        ") VALUES (?,?,?,?,?,'ingest',?,?,?,?,?,?)",
        (
            id_factory(),
            compute_dedup_pair_key(existing_claim_id, new_claim["id"]),
            existing_claim_id,
            new_claim["id"],
            new_claim["id"],
            new_claim["namespace_key"],
            similarity,
            DEDUP_EMBEDDING_TEXT_VERSION,
            DEDUP_POLICY_VERSION,
            new_claim.get("predicate"),
            created_at,
        ),
    )
    return cursor.rowcount == 1
