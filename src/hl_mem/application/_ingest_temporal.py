"""Aggregate deterministic temporal decisions for ingest candidates."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.temporal_links import evaluate_temporal_link


@dataclass(frozen=True)
class _TemporalResolution:
    outcome: str
    representative: dict[str, Any]
    members: tuple[dict[str, Any], ...]
    member_outcomes: tuple[tuple[str, str, str | None, str], ...]
    rationale: str
    snapshot_order: str | None


def _resolve_temporal_candidates(
    members: Sequence[dict[str, Any]], new_claim: dict[str, Any]
) -> _TemporalResolution | None:
    evaluated = [(member, evaluate_temporal_link(member, new_claim)) for member in members]
    actionable = [(member, decision) for member, decision in evaluated if decision.outcome != "not_applicable"]
    if not actionable:
        return None
    competing = [
        (member, decision) for member, decision in actionable if decision.outcome not in {"distinct_series", "unproven"}
    ]
    selected = competing or actionable
    outcomes = {decision.outcome for _, decision in selected}
    outcome = next(iter(outcomes)) if len(outcomes) == 1 else "uncertain"
    if not competing and "unproven" in outcomes:
        outcome = "unproven"
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
