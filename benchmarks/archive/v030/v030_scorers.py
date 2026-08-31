"""Shared deterministic scorers for the frozen v0.30 experiment set."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

_ABSTENTIONS = frozenset({"defer", "l3", "manual_required"})


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Return a two-sided Wilson score interval for a binomial rate."""

    if successes < 0 or total < 0 or successes > total:
        raise ValueError("Wilson counts must satisfy 0 <= successes <= total")
    if total == 0:
        return 0.0, 1.0
    rate = successes / total
    denominator = 1 + z * z / total
    centre = rate + z * z / (2 * total)
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
    lower = 0.0 if successes == 0 else max(0.0, (centre - margin) / denominator)
    upper = 1.0 if successes == total else min(1.0, (centre + margin) / denominator)
    return lower, upper


def _prediction_map(predictions: Sequence[Mapping[str, Any]], case_ids: set[str]) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for prediction in predictions:
        case_id = str(prediction.get("case_id") or "")
        if case_id in indexed:
            raise ValueError(f"duplicate prediction for case {case_id}")
        if case_id not in case_ids:
            raise ValueError(f"prediction references unknown case {case_id}")
        indexed[case_id] = prediction
    return indexed


def _is_exact(gold: Mapping[str, Any], prediction: Mapping[str, Any] | None) -> bool:
    if prediction is None or prediction.get("decision") != gold.get("decision"):
        return False
    if gold.get("decision") == "select_candidate":
        return prediction.get("winner_candidate_key") == gold.get("winner_candidate_key")
    return True


def _is_destructive(gold: Mapping[str, Any], prediction: Mapping[str, Any] | None) -> bool:
    if prediction is None:
        return False
    pair = (gold.get("decision"), prediction.get("decision"))
    if pair in {("keep_left", "keep_right"), ("keep_right", "keep_left")}:
        return True
    return pair == ("select_candidate", "select_candidate") and not _is_exact(gold, prediction)


def score_decisions(cases: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Score coverage, exact agreement, abstention, risk, and source strata."""

    case_ids = [str(case.get("case_id") or "") for case in cases]
    if not all(case_ids) or len(case_ids) != len(set(case_ids)):
        raise ValueError("gold cases require unique non-empty case_id values")
    indexed = _prediction_map(predictions, set(case_ids))
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    exact = abstentions = 0
    destructive: list[str] = []
    for case in cases:
        case_id = str(case["case_id"])
        source = str(case.get("source") or "unknown")
        gold = case.get("gold")
        if not isinstance(gold, Mapping):
            raise ValueError(f"case {case_id} gold must be an object")
        prediction = indexed.get(case_id)
        predicted = str(prediction.get("decision") or "") if prediction else "<missing>"
        expected = str(gold.get("decision") or "")
        confusion[expected][predicted] += 1
        source_counts[source]["covered"] += 0
        source_counts[source]["exact"] += 0
        source_counts[source]["total"] += 1
        if prediction is not None:
            source_counts[source]["covered"] += 1
        if _is_exact(gold, prediction):
            exact += 1
            source_counts[source]["exact"] += 1
        if predicted in _ABSTENTIONS:
            abstentions += 1
        if _is_destructive(gold, prediction):
            destructive.append(case_id)
    total = len(cases)
    covered = len(indexed)
    return {
        "total": total,
        "covered": covered,
        "coverage": covered / total,
        "coverage_interval_95": wilson_interval(covered, total),
        "exact": exact,
        "exact_rate": exact / total,
        "exact_interval_95": wilson_interval(exact, total),
        "abstentions": abstentions,
        "destructive_error_case_ids": sorted(destructive),
        "confusion": {expected: dict(sorted(predicted.items())) for expected, predicted in sorted(confusion.items())},
        "by_source": {source: dict(counts) for source, counts in sorted(source_counts.items())},
    }


def evaluate_decision_gate(
    report: Mapping[str, Any],
    *,
    min_exact: int,
    max_abstentions: int,
    max_destructive: int,
    max_invariant_violations: int,
) -> dict[str, Any]:
    """Evaluate preregistered integer boundaries without rounding rates."""

    failures: list[str] = []
    if int(report.get("exact", 0)) < min_exact:
        failures.append(f"exact<{min_exact}")
    if int(report.get("abstentions", 0)) > max_abstentions:
        failures.append(f"abstentions>{max_abstentions}")
    destructive = report.get("destructive_error_case_ids") or []
    if len(destructive) > max_destructive:
        failures.append(f"destructive>{max_destructive}")
    if int(report.get("invariant_violations", 0)) > max_invariant_violations:
        failures.append(f"invariant>{max_invariant_violations}")
    return {"passed": not failures, "failures": failures}
