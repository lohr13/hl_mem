"""Deterministic v2 corpus derivation for the E5/E6 replay."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from hl_mem.domain.action_coordinates import project_action_qualifiers
from hl_mem.domain.claims.temporal_links import _price_axis, parse_snapshot_coordinate
from hl_mem.domain.instruments import InstrumentReference, resolve_instrument_target

_E5_OUTCOMES = {"complete", "cancel", "replace", "partial", "ambiguous"}
_E5_REQUIRED = {
    "action_family",
    "assertion_phase",
    "canonical_target_entity_id",
    "direction",
    "quantity_mode",
}
_PRICE_REQUIRED = {"price_axis", "canonical_target_entity_id", "snapshot_date"}


def _quantity(value: Mapping[str, Any], default: str = "100") -> str:
    return str(value.get("quantity") or default).replace(",", "")


def _ticker(target: str) -> str:
    suffix = target.rsplit(":", 1)[-1].upper()
    cleaned = "".join(character for character in suffix if character.isalnum()) or "X01"
    return cleaned if cleaned[0].isalpha() and len(cleaned) >= 2 else f"T{cleaned}"


def _instrument_mention(target: str, references: Sequence[InstrumentReference]) -> str:
    reference = next((item for item in references if item.canonical_entity_id == target), None)
    if reference is None:
        return f"NASDAQ:{_ticker(target)}"
    parts = reference.canonical_key.split(":")
    if len(parts) == 3 and parts[0] == "CN":
        return f"{parts[1]}:{parts[2]}"
    if len(parts) == 3 and parts[0] == "US":
        return f"{parts[1]}:{parts[2]}"
    return reference.aliases[0][0] if reference.aliases else f"NASDAQ:{_ticker(target)}"


def _e5_text(target: str, quantity: str, phase: str, references: Sequence[InstrumentReference]) -> str:
    verb = {
        "plan": "plan to buy",
        "execution": "executed buy",
        "cancellation": "cancel buy",
        "replacement": "replace buy",
    }[phase]
    return f"{verb} {quantity} units {_instrument_mention(target, references)}"


def _coordinate(
    raw: Mapping[str, Any],
    *,
    target: str,
    phase: str,
    references: Sequence[InstrumentReference],
) -> dict[str, Any]:
    quantity = _quantity(raw)
    text = _e5_text(target, quantity, phase, references)
    qualifiers = project_action_qualifiers(text, {"account": raw.get("account")}, is_plan=phase == "plan")
    resolved = resolve_instrument_target(text, references)
    return {
        **copy.deepcopy(dict(raw)),
        "text": text,
        "action_family": qualifiers.get("action_family"),
        "direction": qualifiers.get("direction"),
        "assertion_phase": qualifiers.get("assertion_phase"),
        "quantity_mode": qualifiers.get("quantity_mode"),
        "quantity": qualifiers.get("quantity"),
        "unit": qualifiers.get("quantity_unit"),
        "canonical_target_entity_id": resolved.canonical_entity_id,
        "target_resolution": resolved.outcome,
    }


def derive_e5_case(case: Mapping[str, Any], references: Sequence[InstrumentReference]) -> dict[str, Any]:
    """Project one frozen synthetic plan case through the production parsers."""

    derived = copy.deepcopy(dict(case))
    payload = derived.setdefault("input", {})
    plan_raw = payload.get("plan") or {}
    result_raw = payload.get("result") or {}
    target = str(plan_raw.get("target") or result_raw.get("target") or "")
    gold = str((derived.get("gold") or {}).get("decision") or derived.get("category") or "")
    phase = {"cancel": "cancellation", "replace": "replacement"}.get(gold, "execution")
    if gold == "partial":
        result_raw = {**result_raw, "quantity": str(max(1, int(_quantity(plan_raw)) // 2))}
    elif gold in {"ambiguous", "ambiguous_negative"}:
        result_raw = {**result_raw, "quantity": str(int(_quantity(plan_raw)) + 1)}
    payload["plan"] = _coordinate(plan_raw, target=target, phase="plan", references=references)
    payload["result"] = _coordinate(result_raw, target=target, phase=phase, references=references)
    derived["derivation"] = {
        "action_parser": "project_action_qualifiers",
        "target_parser": "resolve_instrument_target",
        "version": "e5-coordinate-v2",
    }
    return derived


def _missing_e5(case: Mapping[str, Any]) -> bool:
    payload = case.get("input") or {}
    plan, result = payload.get("plan") or {}, payload.get("result") or {}
    if not (_E5_REQUIRED <= set(plan) and _E5_REQUIRED <= set(result)) or any(
        plan.get(key) in {None, "", "?"} or result.get(key) in {None, "", "?"} for key in _E5_REQUIRED
    ):
        return True
    return any(
        coordinate.get("quantity_mode") == "exact"
        and (coordinate.get("quantity") in {None, "", "?"} or coordinate.get("unit") in {None, "", "?"})
        for coordinate in (plan, result)
    )


def derive_real_action_claim(
    claim: Mapping[str, Any], references: Sequence[InstrumentReference], *, is_plan: bool
) -> dict[str, Any]:
    """Project one immutable replica claim without interpreting its gold outcome."""

    value = str(claim.get("value") or "")
    subject = str(claim.get("subject_entity_id") or "")
    qualifiers = project_action_qualifiers(value, {}, is_plan=is_plan)
    target = resolve_instrument_target(f"{subject} {value}", references)
    return {
        "claim_id": str(claim.get("id") or ""),
        "text": value,
        "valid_from": claim.get("valid_from"),
        "action_family": qualifiers.get("action_family"),
        "direction": qualifiers.get("direction"),
        "assertion_phase": qualifiers.get("assertion_phase"),
        "quantity_mode": qualifiers.get("quantity_mode"),
        "quantity": qualifiers.get("quantity"),
        "unit": qualifiers.get("quantity_unit"),
        "account": None,
        "canonical_target_entity_id": target.canonical_entity_id,
        "target_resolution": target.outcome,
    }


def assess_e5_v2_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    missing = sum(_missing_e5(case) for case in cases)
    maximum = float((manifest.get("preregistration") or {}).get("max_coordinate_missing_rate", 0.10))
    rate = missing / len(cases) if cases else 1.0
    invalid_gold = sum(str((case.get("gold") or {}).get("decision")) not in _E5_OUTCOMES for case in cases)
    unreconstructable = sum(
        not bool(snapshot.get("reconstructable")) for snapshot in manifest.get("source_snapshots") or []
    )
    blockers = []
    if len(cases) < 140:
        blockers.append("minimum_cases_missing")
    if rate > maximum:
        blockers.append("coordinate_missing_rate_exceeded")
    if invalid_gold:
        blockers.append("invalid_gold_decision")
    if unreconstructable:
        blockers.append("unreconstructable_source_snapshots")
    return {
        "experiment": "E5",
        "ready": not blockers,
        "counts": {
            "cases": len(cases),
            "coordinate_missing_cases": missing,
            "coordinate_missing_rate": rate,
            "invalid_gold_cases": invalid_gold,
            "unreconstructable_source_snapshots": unreconstructable,
        },
        "blockers": blockers,
    }


def _price_coordinate(claim: Mapping[str, Any], references: Sequence[InstrumentReference]) -> dict[str, Any]:
    value = str(claim.get("value") or "")
    subject = str(claim.get("subject_entity_id") or "")
    resolved = resolve_instrument_target(f"{subject} {value}", references)
    snapshot = parse_snapshot_coordinate(value, claim.get("valid_from"))
    return {
        "claim_id": str(claim.get("id") or ""),
        "subject_entity_id": subject,
        "value": value,
        "valid_from": claim.get("valid_from"),
        "price_axis": _price_axis("".join(value.casefold().split())),
        "canonical_target_entity_id": resolved.canonical_entity_id,
        "snapshot_date": snapshot.date().isoformat() if isinstance(snapshot, datetime) else None,
        "target_resolution": resolved.outcome,
    }


def derive_e6_pair(
    case_id: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    references: Sequence[InstrumentReference],
    *,
    source: str,
    label_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one price-pair case with typed targets and three-dimensional coordinates."""

    left_coordinate = _price_coordinate(left, references)
    right_coordinate = _price_coordinate(right, references)
    left_target = left_coordinate["canonical_target_entity_id"]
    right_target = right_coordinate["canonical_target_entity_id"]
    if not left_target or not right_target:
        decision = "uncertain"
    elif left_target != right_target or left_coordinate["price_axis"] != right_coordinate["price_axis"]:
        decision = "distinct_series"
    elif not left_coordinate["snapshot_date"] or not right_coordinate["snapshot_date"]:
        decision = "uncertain"
    elif right_coordinate["snapshot_date"] > left_coordinate["snapshot_date"]:
        decision = "snapshot_advance"
    else:
        decision = "uncertain"
    instrument_id = str(left_target or right_target or f"unresolved:{case_id}")
    return {
        "case_id": case_id,
        "category": "price",
        "source": source,
        "instrument_id": instrument_id,
        "input": {"left": left_coordinate, "right": right_coordinate},
        "gold": {"decision": decision, "label_provenance": copy.deepcopy(dict(label_provenance))},
        "derivation": {
            "axis_parser": "temporal_links._price_axis",
            "date_parser": "parse_snapshot_coordinate",
            "target_parser": "resolve_instrument_target",
            "version": "e6-series-pair-v2",
        },
    }


def assess_e6_v2_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    missing = sum(
        any(not side.get("canonical_target_entity_id") for side in (case.get("input") or {}).values()) for case in cases
    )
    maximum = float((manifest.get("preregistration") or {}).get("max_target_missing_rate", 0.10))
    rate = missing / len(cases) if cases else 1.0
    instruments = {str(case.get("instrument_id") or "") for case in cases} - {""}
    unreconstructable = sum(
        not bool(snapshot.get("reconstructable")) for snapshot in manifest.get("source_snapshots") or []
    )
    incomplete_pairs = sum(
        not all(_PRICE_REQUIRED <= set(side) for side in (case.get("input") or {}).values()) for case in cases
    )
    blockers = []
    if len(cases) < 120:
        blockers.append("minimum_cases_missing")
    if len(instruments) < 52:
        blockers.append("minimum_instruments_missing")
    if rate > maximum:
        blockers.append("target_missing_rate_exceeded")
    if incomplete_pairs:
        blockers.append("series_pair_contract_incomplete")
    if unreconstructable:
        blockers.append("unreconstructable_source_snapshots")
    return {
        "experiment": "E6",
        "ready": not blockers,
        "counts": {
            "cases": len(cases),
            "instruments": len(instruments),
            "target_missing_cases": missing,
            "target_missing_rate": rate,
            "incomplete_series_pairs": incomplete_pairs,
            "unreconstructable_source_snapshots": unreconstructable,
        },
        "blockers": blockers,
    }
