"""Pure matching rules for auditable plan fulfillment."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from hl_mem.domain.action_coordinates import PlanCoordinate, coordinate_from_claim, decimal_text
from hl_mem.domain.governance import snapshot_fingerprint

PLAN_FULFILLMENT_POLICY_VERSION = "plan-fulfillment-v1"


@dataclass(frozen=True, slots=True)
class PlanMatch:
    plan_ids: tuple[str, ...]
    coordinate: PlanCoordinate
    outcome_type: str
    reason: str
    quantity: Decimal | None


def is_result_claim(claim: Mapping[str, Any]) -> bool:
    qualifiers = claim.get("qualifiers")
    return bool(
        isinstance(qualifiers, Mapping)
        and qualifiers.get("assertion_phase") in {"execution", "cancellation", "replacement"}
    )


def coordinate_payload(coordinate: PlanCoordinate) -> dict[str, Any]:
    payload = asdict(coordinate)
    amount = coordinate.quantity.amount
    payload["quantity"]["amount"] = decimal_text(amount) if amount is not None else None
    return payload


def coordinate_hash(coordinate: PlanCoordinate) -> str:
    return snapshot_fingerprint(coordinate_payload(coordinate))


def _same_protected_coordinates(plan: PlanCoordinate, result: PlanCoordinate) -> bool:
    return bool(
        plan.namespace == result.namespace
        and plan.canonical_target_entity_id == result.canonical_target_entity_id
        and plan.action_family == result.action_family
        and plan.direction == result.direction
        and plan.account == result.account
        and plan.quantity.mode == result.quantity.mode
        and plan.quantity.unit == result.quantity.unit
    )


def _same_anchor_coordinates(plan: PlanCoordinate, result: PlanCoordinate) -> bool:
    return bool(
        plan.namespace == result.namespace
        and plan.canonical_target_entity_id == result.canonical_target_entity_id
        and plan.action_family == result.action_family
        and plan.direction == result.direction
        and plan.account == result.account
    )


def _within_window(plan: PlanCoordinate, result: PlanCoordinate) -> bool:
    return result.window_start >= plan.window_start and (
        plan.window_end is None or result.window_start <= plan.window_end
    )


def strict_candidate(plan_claim: Mapping[str, Any], result_claim: Mapping[str, Any]) -> bool:
    plan = coordinate_from_claim(plan_claim)
    result = coordinate_from_claim(result_claim)
    if plan is None or result is None:
        return False
    return bool(
        plan.assertion_phase == "plan"
        and result.assertion_phase != "plan"
        and _same_protected_coordinates(plan, result)
        and _within_window(plan, result)
    )


def _logical_groups(
    claims: list[Mapping[str, Any]], equivalent_pairs: Iterable[tuple[str, str]]
) -> list[list[Mapping[str, Any]]]:
    by_id = {str(claim["id"]): claim for claim in claims}
    parent = {claim_id: claim_id for claim_id in by_id}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    hashes: dict[str, str] = {}
    for claim in claims:
        claim_id = str(claim["id"])
        fact_hash = str(claim.get("fact_hash") or "")
        if fact_hash and fact_hash in hashes:
            union(claim_id, hashes[fact_hash])
        elif fact_hash:
            hashes[fact_hash] = claim_id
    for left, right in equivalent_pairs:
        if left in by_id and right in by_id:
            union(left, right)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for claim_id, claim in by_id.items():
        groups.setdefault(find(claim_id), []).append(claim)
    return list(groups.values())


def _outcome(plan: PlanCoordinate, result: PlanCoordinate) -> tuple[str, str]:
    if result.assertion_phase == "cancellation":
        return "cancel", "strict_cancellation"
    if result.assertion_phase == "replacement":
        return "replace", "strict_replacement"
    if plan.quantity.mode == "all" and result.quantity.mode == "all":
        return "complete", "strict_all_execution"
    plan_amount, result_amount = plan.quantity.amount, result.quantity.amount
    if plan_amount is None or result_amount is None:
        return "ambiguous", "quantity_incomplete"
    if result_amount > plan_amount:
        return "ambiguous", "partial_overfill"
    if result_amount == plan_amount:
        return "complete", "strict_exact_execution"
    return "partial", "strict_partial_execution"


def _required_coordinate(claim: Mapping[str, Any]) -> PlanCoordinate:
    coordinate = coordinate_from_claim(claim)
    if coordinate is None:
        raise ValueError("strict candidate lost its plan coordinate")
    return coordinate


def select_plan_match(
    candidates: Sequence[Mapping[str, Any]],
    result_claim: Mapping[str, Any],
    equivalent_pairs: Iterable[tuple[str, str]] = (),
) -> PlanMatch | None:
    """Select a unique logical plan group; never ask a model to choose among groups."""

    qualifiers = result_claim.get("qualifiers")
    phase = qualifiers.get("assertion_phase") if isinstance(qualifiers, Mapping) else None
    strong_plan_id = qualifiers.get("plan_claim_id") if isinstance(qualifiers, Mapping) else None
    strong_anchor = bool(strong_plan_id and phase in {"cancellation", "replacement"})
    result = coordinate_from_claim(result_claim, allow_unknown_quantity=strong_anchor)
    if result is None or result.assertion_phase == "plan":
        return None
    if strong_anchor:
        matching = [
            claim
            for claim in candidates
            if str(claim["id"]) == str(strong_plan_id)
            and (plan := coordinate_from_claim(claim)) is not None
            and _same_anchor_coordinates(plan, result)
            and _within_window(plan, result)
        ]
    else:
        matching = [claim for claim in candidates if strict_candidate(claim, result_claim)]
    if not matching:
        return None
    groups = _logical_groups(matching, equivalent_pairs)
    if len(groups) != 1:
        return PlanMatch((), _required_coordinate(matching[0]), "ambiguous", "ambiguous_multiple_groups", None)
    group = sorted(groups[0], key=lambda item: str(item["id"]))
    coordinates = [coordinate_from_claim(claim) for claim in group]
    if any(item is None for item in coordinates) or len(set(coordinates)) != 1:
        return PlanMatch((), _required_coordinate(group[0]), "ambiguous", "protected_coordinate_mismatch", None)
    plan = coordinates[0]
    assert plan is not None
    outcome_type, reason = _outcome(plan, result)
    return PlanMatch(
        tuple(str(claim["id"]) for claim in group),
        plan,
        outcome_type,
        reason,
        result.quantity.amount,
    )
