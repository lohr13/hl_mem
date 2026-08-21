"""Structured scorer for the frozen state-coordinate A/B protocol."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Collection, Hashable, Mapping, Sequence, Set
from pathlib import Path
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


def classification_metrics(
    gold: Set[Hashable] | Collection[Hashable],
    predicted: Set[Hashable] | Collection[Hashable],
) -> dict[str, int | float]:
    """Return exact-set precision/recall/F1 with a perfect empty/empty score."""

    gold_set = set(gold)
    predicted_set = set(predicted)
    true_positive = len(gold_set & predicted_set)
    false_positive = len(predicted_set - gold_set)
    false_negative = len(gold_set - predicted_set)
    precision = true_positive / len(predicted_set) if predicted_set else float(not gold_set)
    recall = true_positive / len(gold_set) if gold_set else float(not predicted_set)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _open_readonly(path_value: str | Path) -> sqlite3.Connection:
    path = Path(path_value).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_persisted_edges(
    database_path: str | Path,
    claim_manifest: Mapping[str, str],
) -> dict[str, Any]:
    """Load real supersede edges from a read-only experiment database.

    ``claim_manifest`` maps stored claim ids to frozen assertion ids. Audit text
    and inferred lifecycle language are deliberately outside this boundary.
    """

    connection = _open_readonly(database_path)
    try:
        connection.execute("BEGIN")
        claim_columns = _table_columns(connection, "claims")
        missing = {"id", "superseded_by_id"} - claim_columns
        if missing:
            raise ValueError(f"claims table is missing structured edge columns: {', '.join(sorted(missing))}")
        raw_claim_edges = {
            (str(row["id"]), str(row["superseded_by_id"]))
            for row in connection.execute("SELECT id,superseded_by_id FROM claims WHERE superseded_by_id IS NOT NULL")
        }
        evidence_columns = _table_columns(connection, "evidence_links")
        if {"derived_id", "evidence_id", "relation", "derived_type", "evidence_type"} <= evidence_columns:
            raw_evidence_edges = {
                (str(row["evidence_id"]), str(row["derived_id"]))
                for row in connection.execute(
                    "SELECT derived_id,evidence_id FROM evidence_links "
                    "WHERE relation='supersedes' AND derived_type='claim' AND evidence_type='claim'"
                )
            }
        else:
            raw_evidence_edges = set()
        raw_edges = raw_claim_edges | raw_evidence_edges
        edges = {
            (claim_manifest[old_id], claim_manifest[new_id])
            for old_id, new_id in raw_edges
            if old_id in claim_manifest and new_id in claim_manifest
        }
        unknown_endpoint_edges = sum(
            old_id not in claim_manifest or new_id not in claim_manifest for old_id, new_id in raw_edges
        )
        return {
            "edges": edges,
            "sources": {
                "claims.superseded_by_id": len(raw_claim_edges),
                "evidence_links": len(raw_evidence_edges),
            },
            "unknown_endpoint_edges": unknown_endpoint_edges,
        }
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _coordinate_key(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _gold_projection(gold_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    atomic_ids: set[str] = set()
    coordinates: dict[str, str] = {}
    non_state_ids: set[str] = set()
    expected_edges: set[tuple[str, str]] = set()
    current_ids: set[str] = set()
    historical_ids: set[str] = set()
    counterexample_ids: set[str] = set()
    counterexample_samples: dict[str, str] = {}
    for record in gold_records:
        sample_id = str(record.get("sample_id") or record.get("bundle_id") or "")
        claims = record.get("atomic_claims")
        if not isinstance(claims, Sequence):
            raise ValueError(f"gold {sample_id} atomic_claims must be an array")
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise ValueError(f"gold {sample_id} atomic_claims must contain objects")
            assertion_id = str(claim.get("assertion_id") or "")
            if not assertion_id or assertion_id in atomic_ids:
                raise ValueError(f"gold assertion_id must be non-blank and unique: {assertion_id!r}")
            atomic_ids.add(assertion_id)
            coordinate = claim.get("coordinate")
            if isinstance(coordinate, Mapping):
                coordinates[assertion_id] = _coordinate_key(coordinate)
            else:
                non_state_ids.add(assertion_id)
            if record.get("counterexample_zero_supersede") is True:
                counterexample_ids.add(assertion_id)
                counterexample_samples[assertion_id] = sample_id
        for edge in record.get("expected_supersede_edges") or ():
            if not isinstance(edge, Sequence) or len(edge) != 2:
                raise ValueError(f"gold {sample_id} supersede edges must be pairs")
            expected_edges.add((str(edge[0]), str(edge[1])))
        current_ids.update(str(value) for value in record.get("current_assertion_ids") or ())
        historical_ids.update(str(value) for value in record.get("historical_assertion_ids") or ())
    return {
        "atomic_ids": atomic_ids,
        "coordinates": coordinates,
        "non_state_ids": non_state_ids,
        "expected_edges": expected_edges,
        "current_ids": current_ids,
        "historical_ids": historical_ids,
        "counterexample_ids": counterexample_ids,
        "counterexample_samples": counterexample_samples,
    }


def _run_projection(run: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    atomic_ids: set[str] = set()
    coordinates: dict[str, str] = {}
    non_state_ids: set[str] = set()
    coordinate_occurrences: list[tuple[str, str]] = []
    claim_count = 0
    for sample in run:
        claims = sample.get("claims")
        if not isinstance(claims, Sequence):
            raise ValueError("candidate run claims must be an array")
        claim_count += len(claims)
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise ValueError("candidate run claims must contain objects")
            assertion_id = str(claim.get("assertion_id") or "")
            if not assertion_id or assertion_id in atomic_ids:
                raise ValueError(f"candidate assertion_id must be non-blank and unique: {assertion_id!r}")
            atomic_ids.add(assertion_id)
            projection = claim.get("projection")
            coordinate = projection.get("coordinate") if isinstance(projection, Mapping) else None
            if isinstance(coordinate, Mapping):
                coordinate_key = _coordinate_key(coordinate)
                coordinates[assertion_id] = coordinate_key
                coordinate_occurrences.append((str(sample.get("sample_id") or ""), coordinate_key))
            else:
                non_state_ids.add(assertion_id)
    return {
        "atomic_ids": atomic_ids,
        "coordinates": coordinates,
        "non_state_ids": non_state_ids,
        "coordinate_occurrences": coordinate_occurrences,
        "claim_count": claim_count,
    }


def _coordinate_metrics(gold: Mapping[str, str], predicted: Mapping[str, str]) -> dict[str, int | float]:
    gold_pairs = {(assertion_id, coordinate) for assertion_id, coordinate in gold.items()}
    predicted_pairs = {(assertion_id, coordinate) for assertion_id, coordinate in predicted.items()}
    return classification_metrics(gold_pairs, predicted_pairs)


def _stale_rate(expected: set[str], injected: Collection[object]) -> float:
    injected_ids = [str(value) for value in injected]
    return (
        sum(assertion_id not in expected for assertion_id in injected_ids) / len(injected_ids) if injected_ids else 0.0
    )


def _reduction(baseline: float, candidate: float) -> float:
    if baseline == 0.0:
        return 1.0 if candidate == 0.0 else 0.0
    return (baseline - candidate) / baseline


def _inflation(baseline: int, candidate: int) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else float("inf")
    return (candidate - baseline) / baseline


def _three_run_consistency(projections: Sequence[Sequence[tuple[str, str]]]) -> float:
    counters = [Counter(projection) for projection in projections]
    denominator = max((sum(counter.values()) for counter in counters), default=0)
    if denominator == 0:
        return 1.0
    reference = counters[0]
    consistent = sum(min(counter[coordinate] for counter in counters) for coordinate in reference)
    return consistent / denominator


def _check(operator: str, actual: float | int, target: float | int) -> bool:
    if operator == ">=":
        return actual >= target
    if operator == "<=":
        return actual <= target
    raise ValueError(f"unsupported threshold operator: {operator}")


def score_protocol(
    gold_records: Sequence[Mapping[str, Any]],
    *,
    baseline_predictions: Mapping[str, Any],
    candidate_runs: Sequence[Sequence[Mapping[str, Any]]],
    persisted_edges: Collection[tuple[str, str]],
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
) -> dict[str, Any]:
    """Score one candidate against all frozen structural pass lines."""

    if len(candidate_runs) != 3:
        raise ValueError("three candidate runs are required for coordinate consistency")
    gold = _gold_projection(gold_records)
    candidate_projections = [_run_projection(run) for run in candidate_runs]
    candidate = candidate_projections[0]
    atomic_metrics = classification_metrics(gold["atomic_ids"], candidate["atomic_ids"])
    coordinate_metrics = _coordinate_metrics(gold["coordinates"], candidate["coordinates"])
    edge_metrics = classification_metrics(gold["expected_edges"], set(persisted_edges))

    cross_coordinate_edges = 0
    for old_id, new_id in persisted_edges:
        if not (
            old_id in gold["counterexample_ids"]
            and new_id in gold["counterexample_ids"]
            and gold["counterexample_samples"].get(old_id) == gold["counterexample_samples"].get(new_id)
        ):
            continue
        old_coordinate = gold["coordinates"].get(old_id)
        new_coordinate = gold["coordinates"].get(new_id)
        if old_coordinate is not None and new_coordinate is not None and old_coordinate != new_coordinate:
            cross_coordinate_edges += 1

    baseline_rate = _stale_rate(gold["current_ids"], baseline_observations.get("current_injected_assertion_ids") or ())
    candidate_rate = _stale_rate(
        gold["current_ids"], candidate_observations.get("current_injected_assertion_ids") or ()
    )
    historical_metrics = classification_metrics(
        gold["historical_ids"],
        {str(value) for value in candidate_observations.get("historical_retrieved_assertion_ids") or ()},
    )
    baseline_non_state = classification_metrics(
        gold["non_state_ids"],
        {str(value) for value in baseline_predictions.get("non_state_assertion_ids") or ()},
    )
    candidate_non_state = classification_metrics(gold["non_state_ids"], candidate["non_state_ids"])
    consistency = _three_run_consistency([projection["coordinate_occurrences"] for projection in candidate_projections])
    claim_inflation = _inflation(int(baseline_predictions.get("claim_count") or 0), candidate["claim_count"])
    non_state_f1_drop = float(baseline_non_state["f1"]) - float(candidate_non_state["f1"])

    stale_reduction = _reduction(baseline_rate, candidate_rate)
    metrics = {
        "state_coordinate": coordinate_metrics,
        "atomic_claim": atomic_metrics,
        "supersede_edge": edge_metrics,
        "counterexample_cross_coordinate_supersede": cross_coordinate_edges,
        "current_state_stale_injection": {
            "baseline_rate": baseline_rate,
            "candidate_rate": candidate_rate,
            "reduction": stale_reduction,
        },
        "historical_old_snapshot_recall": historical_metrics["recall"],
        "non_state_extraction": {
            "baseline_f1": baseline_non_state["f1"],
            "candidate_f1": candidate_non_state["f1"],
            "f1_drop": non_state_f1_drop,
        },
        "claim_inflation": claim_inflation,
        "three_run_coordinate_consistency": consistency,
    }
    actuals: dict[str, float | int] = {
        "state_coordinate_precision": float(coordinate_metrics["precision"]),
        "state_coordinate_recall": float(coordinate_metrics["recall"]),
        "atomic_claim_precision": float(atomic_metrics["precision"]),
        "atomic_claim_recall": float(atomic_metrics["recall"]),
        "supersede_edge_precision": float(edge_metrics["precision"]),
        "supersede_edge_recall": float(edge_metrics["recall"]),
        "counterexample_cross_coordinate_supersede": cross_coordinate_edges,
        "stale_injection_reduction": stale_reduction,
        "stale_injection_absolute": candidate_rate,
        "historical_old_snapshot_recall": float(historical_metrics["recall"]),
        "non_state_f1_drop": non_state_f1_drop,
        "claim_inflation": claim_inflation,
        "three_run_coordinate_consistency": consistency,
    }
    checks = {
        name: {
            "actual": actuals[name],
            **threshold,
            "passed": _check(str(threshold["operator"]), actuals[name], threshold["target"]),
        }
        for name, threshold in THRESHOLDS.items()
    }
    return {
        "schema_version": 1,
        "metrics": metrics,
        "thresholds": {name: dict(value) for name, value in THRESHOLDS.items()},
        "checks": checks,
        "passed": all(check["passed"] for check in checks.values()),
    }


def score_protocol_file(
    gold_path: str | Path,
    *,
    baseline_predictions: Mapping[str, Any],
    candidate_runs: Sequence[Sequence[Mapping[str, Any]]],
    persisted_edges: Collection[tuple[str, str]],
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
) -> dict[str, Any]:
    """Consume dev or sealed gold internally and return aggregate metrics only."""

    records: list[Mapping[str, Any]] = []
    for line_index, line in enumerate(Path(gold_path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError(f"gold JSONL line {line_index} must be an object")
        records.append(value)
    return score_protocol(
        records,
        baseline_predictions=baseline_predictions,
        candidate_runs=candidate_runs,
        persisted_edges=persisted_edges,
        baseline_observations=baseline_observations,
        candidate_observations=candidate_observations,
    )
