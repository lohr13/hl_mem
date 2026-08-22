"""Frozen thresholds and their integer satisfiability audit."""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any

THRESHOLDS: dict[str, dict[str, Any]] = {
    "state_coordinate_precision": {"operator": ">=", "target": 0.99},
    "state_coordinate_recall": {"operator": ">=", "target": 0.95},
    "atomic_claim_precision": {"operator": ">=", "target": 0.98},
    "atomic_claim_recall": {"operator": ">=", "target": 0.95},
    "supersede_edge_precision": {"operator": ">=", "target": 1.0},
    "supersede_edge_recall": {"operator": ">=", "target": 0.95},
    "counterexample_cross_coordinate_supersede": {"operator": "<=", "target": 0},
    "stale_injection_reduction": {"operator": ">=", "target": 0.90},
    "stale_injection_absolute": {"operator": "<=", "target": 0.01},
    "historical_old_snapshot_recall": {"operator": ">=", "target": 1.0},
    "non_state_f1_drop": {"operator": "<=", "target": 0.01},
    "claim_inflation": {"operator": "<=", "target": 0.05},
    "three_run_coordinate_consistency": {"operator": ">=", "target": 0.99},
}


def check_threshold_satisfiability(
    *,
    gold_atomic_count: int,
    gold_coordinate_count: int,
    gold_edge_count: int,
    historical_assertion_count: int,
    baseline_claim_count: int,
) -> dict[str, Any]:
    """Audit all threshold pairs for incompatible integer count bounds."""

    counts = {
        "gold_atomic_count": gold_atomic_count,
        "gold_coordinate_count": gold_coordinate_count,
        "gold_edge_count": gold_edge_count,
        "historical_assertion_count": historical_assertion_count,
        "baseline_claim_count": baseline_claim_count,
    }
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ValueError("threshold satisfiability counts must be non-negative integers")

    bounds: dict[str, dict[str, dict[str, int]]] = {name: {} for name in THRESHOLDS}
    bounds["state_coordinate_recall"]["candidate_claim_count"] = {
        "lower": math.ceil(gold_coordinate_count * float(THRESHOLDS["state_coordinate_recall"]["target"]))
    }
    bounds["atomic_claim_recall"]["candidate_claim_count"] = {
        "lower": math.ceil(gold_atomic_count * float(THRESHOLDS["atomic_claim_recall"]["target"]))
    }
    bounds["supersede_edge_recall"]["candidate_edge_count"] = {
        "lower": math.ceil(gold_edge_count * float(THRESHOLDS["supersede_edge_recall"]["target"]))
    }
    bounds["historical_old_snapshot_recall"]["candidate_claim_count"] = {
        "lower": math.ceil(historical_assertion_count * float(THRESHOLDS["historical_old_snapshot_recall"]["target"]))
    }
    inflation_target = float(THRESHOLDS["claim_inflation"]["target"])
    bounds["claim_inflation"]["unmatched_candidate_count"] = {"upper": math.floor(gold_atomic_count * inflation_target)}

    pair_results: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for left, right in combinations(THRESHOLDS, 2):
        shared_quantities = sorted(set(bounds[left]) & set(bounds[right]))
        pair: dict[str, Any] = {
            "thresholds": [left, right],
            "shared_quantities": shared_quantities,
            "satisfiable": True,
        }
        for quantity in shared_quantities:
            left_bound = bounds[left][quantity]
            right_bound = bounds[right][quantity]
            lower = max(left_bound.get("lower", 0), right_bound.get("lower", 0))
            upper_values = [value for value in (left_bound.get("upper"), right_bound.get("upper")) if value is not None]
            upper = min(upper_values) if upper_values else None
            if upper is None or lower <= upper:
                continue
            minimum_inflation = (
                (lower - baseline_claim_count) / baseline_claim_count
                if quantity == "candidate_claim_count" and baseline_claim_count
                else float("inf")
            )
            conflict = {
                "thresholds": [left, right],
                "shared_quantity": quantity,
                "lower_bound": lower,
                "upper_bound": upper,
                "minimum_compatible_claim_inflation": minimum_inflation,
                "recommendation": (
                    "Use a comparable complete atomic baseline or a duplicate/spurious atomic-rate gate; "
                    "changing the frozen threshold requires user approval."
                ),
            }
            pair["satisfiable"] = False
            pair["conflict"] = conflict
            conflicts.append(conflict)
        pair_results.append(pair)
    return {
        "threshold_count": len(THRESHOLDS),
        "pairs_checked": len(pair_results),
        "satisfiable": not conflicts,
        "bounds": bounds,
        "pair_results": pair_results,
        "conflicts": conflicts,
    }


def threshold_passes(operator: str, actual: float | int, target: float | int) -> bool:
    if operator == ">=":
        return actual >= target
    if operator == "<=":
        return actual <= target
    raise ValueError(f"unsupported threshold operator: {operator}")
