"""Structured scorer for the frozen state-coordinate A/B protocol."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Collection, Hashable, Mapping, Sequence, Set
from itertools import combinations
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
    bounds["claim_inflation"]["candidate_claim_count"] = {
        "upper": math.floor(baseline_claim_count * (1.0 + inflation_target))
    }

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


def _normalized_text(value: object) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKC", str(value)).casefold() if not character.isspace()
    )


def _content_anchors(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    ascii_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text)
        if len(token) >= 3 or any(character.isdigit() for character in token)
    }
    han_anchors = {
        run[index : index + 2] for run in re.findall(r"[\u3400-\u9fff]+", text) for index in range(len(run) - 1)
    }
    return ascii_tokens | han_anchors


def _source_indices(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} source_event_indices must be an integer array")
    indices = list(value)
    if not indices or any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices):
        raise ValueError(f"{label} source_event_indices must be a non-empty non-negative integer array")
    return tuple(sorted(set(indices)))


def _event_text(event: Mapping[str, Any]) -> str:
    content = event.get("content")
    if isinstance(content, Mapping):
        return str(content.get("text") or "")
    return str(content or "")


def _corpus_projection(corpus_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {}
    for record in corpus_records:
        sample_id = str(record.get("sample_id") or record.get("bundle_id") or "").strip()
        if not sample_id or sample_id in samples:
            raise ValueError(f"corpus sample id must be non-blank and unique: {sample_id!r}")
        events = record.get("events")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise ValueError(f"corpus {sample_id} events must be an array")
        normalized_events: dict[int, str] = {}
        for fallback_index, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise ValueError(f"corpus {sample_id} events must contain objects")
            event_index = event.get("event_index", fallback_index)
            if isinstance(event_index, bool) or not isinstance(event_index, int) or event_index < 0:
                raise ValueError(f"corpus {sample_id} event_index must be a non-negative integer")
            if event_index in normalized_events:
                raise ValueError(f"corpus {sample_id} event_index must be unique: {event_index}")
            normalized_events[event_index] = _normalized_text(_event_text(event))
        samples[sample_id] = {
            "category": str(record.get("category") or "unknown"),
            "subtype": str(record.get("subtype") or "unknown"),
            "events": normalized_events,
        }
    return samples


def _gold_projection(gold_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    atomic_ids: set[str] = set()
    assertions: dict[str, dict[str, Any]] = {}
    by_sample: dict[str, list[str]] = {}
    sample_metadata: dict[str, dict[str, str]] = {}
    coordinates: dict[str, str] = {}
    non_state_ids: set[str] = set()
    expected_edges: set[tuple[str, str]] = set()
    current_ids: set[str] = set()
    historical_ids: set[str] = set()
    counterexample_ids: set[str] = set()
    counterexample_samples: dict[str, str] = {}
    for record in gold_records:
        sample_id = str(record.get("sample_id") or record.get("bundle_id") or "").strip()
        if not sample_id or sample_id in by_sample:
            raise ValueError(f"gold sample id must be non-blank and unique: {sample_id!r}")
        claims = record.get("atomic_claims")
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
            raise ValueError(f"gold {sample_id} atomic_claims must be an array")
        by_sample[sample_id] = []
        sample_metadata[sample_id] = {
            "category": str(record.get("category") or "unknown"),
            "subtype": str(record.get("subtype") or "unknown"),
        }
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise ValueError(f"gold {sample_id} atomic_claims must contain objects")
            assertion_id = str(claim.get("assertion_id") or "")
            if not assertion_id or assertion_id in atomic_ids:
                raise ValueError(f"gold assertion_id must be non-blank and unique: {assertion_id!r}")
            atomic_ids.add(assertion_id)
            source_indices = _source_indices(claim.get("source_event_indices"), f"gold {assertion_id}")
            state_value = str(claim.get("state_value") or "").strip()
            if not state_value:
                raise ValueError(f"gold {assertion_id} state_value must be non-blank")
            assertions[assertion_id] = {
                "assertion_id": assertion_id,
                "sample_id": sample_id,
                "source_event_indices": source_indices,
                "state_value": state_value,
            }
            by_sample[sample_id].append(assertion_id)
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
        "assertions": assertions,
        "by_sample": by_sample,
        "sample_metadata": sample_metadata,
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
    assertions: dict[str, dict[str, Any]] = {}
    by_sample: dict[str, list[str]] = {}
    coordinates: dict[str, str] = {}
    non_state_ids: set[str] = set()
    coordinate_occurrences: list[tuple[str, str]] = []
    claim_count = 0
    for sample in run:
        if not isinstance(sample, Mapping):
            raise ValueError("candidate run samples must be objects")
        sample_id = str(sample.get("sample_id") or "").strip()
        if not sample_id or sample_id in by_sample:
            raise ValueError(f"candidate sample id must be non-blank and unique: {sample_id!r}")
        claims = sample.get("claims")
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes)):
            raise ValueError("candidate run claims must be an array")
        by_sample[sample_id] = []
        claim_count += len(claims)
        for claim in claims:
            if not isinstance(claim, Mapping):
                raise ValueError("candidate run claims must contain objects")
            assertion_id = str(claim.get("assertion_id") or "")
            if not assertion_id or assertion_id in atomic_ids:
                raise ValueError(f"candidate assertion_id must be non-blank and unique: {assertion_id!r}")
            atomic_ids.add(assertion_id)
            raw_claim = claim.get("claim")
            if not isinstance(raw_claim, Mapping):
                raise ValueError(f"candidate {assertion_id} claim must be an object")
            source_indices = _source_indices(raw_claim.get("source_event_indices"), f"candidate {assertion_id}")
            value = str(raw_claim.get("value") or "").strip()
            evidence_quote = str(raw_claim.get("evidence_quote") or "").strip()
            if not value or not evidence_quote:
                raise ValueError(f"candidate {assertion_id} value and evidence_quote must be non-blank")
            projection = claim.get("projection")
            coordinate = projection.get("coordinate") if isinstance(projection, Mapping) else None
            assertions[assertion_id] = {
                "assertion_id": assertion_id,
                "sample_id": sample_id,
                "source_event_indices": source_indices,
                "value": value,
                "evidence_quote": evidence_quote,
            }
            by_sample[sample_id].append(assertion_id)
            if isinstance(coordinate, Mapping):
                coordinate_key = _coordinate_key(coordinate)
                coordinates[assertion_id] = coordinate_key
                coordinate_occurrences.append((sample_id, coordinate_key))
            else:
                non_state_ids.add(assertion_id)
    return {
        "atomic_ids": atomic_ids,
        "assertions": assertions,
        "by_sample": by_sample,
        "coordinates": coordinates,
        "non_state_ids": non_state_ids,
        "coordinate_occurrences": coordinate_occurrences,
        "claim_count": claim_count,
    }


def _semantic_match_reason(
    gold_assertion: Mapping[str, Any],
    candidate_assertion: Mapping[str, Any],
    corpus_sample: Mapping[str, Any] | None,
) -> str | None:
    if gold_assertion["source_event_indices"] != candidate_assertion["source_event_indices"]:
        return "source_event_mismatch"
    state_value = _normalized_text(gold_assertion["state_value"])
    value = _normalized_text(candidate_assertion["value"])
    evidence = _normalized_text(candidate_assertion["evidence_quote"])
    literal_state_value = True
    if corpus_sample is not None:
        events = corpus_sample["events"]
        selected_events = [events.get(index, "") for index in candidate_assertion["source_event_indices"]]
        if any(not event for event in selected_events):
            return "source_event_mismatch"
        source_text = "\n".join(selected_events)
        if not evidence or evidence not in source_text:
            return "evidence_ungrounded"
        literal_state_value = bool(state_value and state_value in source_text)
    if literal_state_value and state_value not in value:
        return "state_value_mismatch"
    if literal_state_value and state_value not in evidence:
        return "evidence_value_mismatch"
    if not literal_state_value and not (
        _content_anchors(candidate_assertion["value"]) & _content_anchors(candidate_assertion["evidence_quote"])
    ):
        return "value_evidence_mismatch"
    return None


def _match_assertions(
    gold: Mapping[str, Any],
    candidate: Mapping[str, Any],
    corpus: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_to_gold: dict[str, str] = {}
    semantic_rejections: Counter[str] = Counter()
    gold_zero_false_positives = 0
    for sample_id in sorted(set(gold["by_sample"]) | set(candidate["by_sample"])):
        gold_ids = sorted(gold["by_sample"].get(sample_id, ()))
        candidate_ids = sorted(candidate["by_sample"].get(sample_id, ()))
        corpus_sample = corpus.get(sample_id)
        adjacency: dict[str, list[str]] = {}
        reasons: dict[tuple[str, str], str] = {}
        for candidate_id in candidate_ids:
            compatible: list[str] = []
            for gold_id in gold_ids:
                reason = _semantic_match_reason(
                    gold["assertions"][gold_id], candidate["assertions"][candidate_id], corpus_sample
                )
                if reason is None:
                    compatible.append(gold_id)
                else:
                    reasons[(candidate_id, gold_id)] = reason
            adjacency[candidate_id] = compatible

        gold_to_candidate: dict[str, str] = {}

        def assign(candidate_id: str, visited: set[str]) -> bool:
            for gold_id in adjacency[candidate_id]:
                if gold_id in visited:
                    continue
                visited.add(gold_id)
                previous = gold_to_candidate.get(gold_id)
                if previous is None or assign(previous, visited):
                    gold_to_candidate[gold_id] = candidate_id
                    return True
            return False

        for candidate_id in sorted(candidate_ids, key=lambda value: (len(adjacency[value]), value)):
            assign(candidate_id, set())
        for gold_id, candidate_id in gold_to_candidate.items():
            candidate_to_gold[candidate_id] = gold_id
        unmatched_candidates = [candidate_id for candidate_id in candidate_ids if candidate_id not in candidate_to_gold]
        if not gold_ids:
            gold_zero_false_positives += len(unmatched_candidates)
            semantic_rejections["gold_zero"] += len(unmatched_candidates)
            continue
        reason_priority = (
            "evidence_ungrounded",
            "state_value_mismatch",
            "evidence_value_mismatch",
            "value_evidence_mismatch",
            "source_event_mismatch",
        )
        for candidate_id in unmatched_candidates:
            if adjacency[candidate_id]:
                semantic_rejections["duplicate_semantic_match"] += 1
                continue
            candidate_reasons = {reasons[(candidate_id, gold_id)] for gold_id in gold_ids}
            semantic_rejections[next(reason for reason in reason_priority if reason in candidate_reasons)] += 1
    matched_gold = set(candidate_to_gold.values())
    return {
        "candidate_to_gold": candidate_to_gold,
        "unmatched_candidate_ids": set(candidate["atomic_ids"]) - set(candidate_to_gold),
        "unmatched_gold_ids": set(gold["atomic_ids"]) - matched_gold,
        "matched_gold_ids": matched_gold,
        "layout_remapped_matches": sum(candidate_id != gold_id for candidate_id, gold_id in candidate_to_gold.items()),
        "identity_matches": sum(candidate_id == gold_id for candidate_id, gold_id in candidate_to_gold.items()),
        "semantic_rejections": dict(sorted(semantic_rejections.items())),
        "gold_zero_false_positives": gold_zero_false_positives,
    }


def _metrics_from_counts(true_positive: int, false_positive: int, false_negative: int) -> dict[str, int | float]:
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else float(false_negative == 0)
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else float(false_positive == 0)
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


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
    baseline_run: Sequence[Mapping[str, Any]] | None = None,
    corpus_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Score one candidate against all frozen structural pass lines."""

    if len(candidate_runs) != 3:
        raise ValueError("three candidate runs are required for coordinate consistency")
    gold = _gold_projection(gold_records)
    if not corpus_records:
        raise ValueError("corpus_records are required for semantic protocol scoring")
    corpus = _corpus_projection(corpus_records)
    candidate_projections = [_run_projection(run) for run in candidate_runs]
    candidate = candidate_projections[0]
    baseline_projection = _run_projection(baseline_run) if baseline_run is not None else None
    declared_baseline_count = baseline_predictions.get("claim_count")
    if (
        isinstance(declared_baseline_count, bool)
        or not isinstance(declared_baseline_count, int)
        or declared_baseline_count < 0
    ):
        raise ValueError("baseline claim_count must be a non-negative integer")
    if baseline_projection is not None and declared_baseline_count != baseline_projection["claim_count"]:
        raise ValueError("baseline claim_count does not match baseline_run")
    baseline_claim_count = (
        int(baseline_projection["claim_count"]) if baseline_projection is not None else declared_baseline_count
    )
    required_corpus_samples = set(gold["by_sample"]) | set().union(
        *(set(projection["by_sample"]) for projection in candidate_projections)
    )
    if baseline_projection is not None:
        required_corpus_samples.update(baseline_projection["by_sample"])
    missing_corpus_samples = sorted(required_corpus_samples - set(corpus))
    if missing_corpus_samples:
        raise ValueError(f"corpus_records missing {len(missing_corpus_samples)} required sample(s)")
    match = _match_assertions(gold, candidate, corpus)
    candidate_to_gold = match["candidate_to_gold"]
    baseline_match = _match_assertions(gold, baseline_projection, corpus) if baseline_projection is not None else None
    baseline_to_gold = baseline_match["candidate_to_gold"] if baseline_match is not None else {}
    atomic_metrics = _metrics_from_counts(
        len(candidate_to_gold),
        len(match["unmatched_candidate_ids"]),
        len(match["unmatched_gold_ids"]),
    )

    sample_counts: dict[str, dict[str, Counter[str]]] = {}

    def add_counts(
        sample_id: str,
        metric: str,
        *,
        true_positive: int = 0,
        false_positive: int = 0,
        false_negative: int = 0,
    ) -> None:
        metrics = sample_counts.setdefault(sample_id, {})
        counts = metrics.setdefault(metric, Counter())
        counts["true_positive"] += true_positive
        counts["false_positive"] += false_positive
        counts["false_negative"] += false_negative

    for sample_id in sorted(set(gold["by_sample"]) | set(candidate["by_sample"])):
        gold_ids = set(gold["by_sample"].get(sample_id, ()))
        candidate_ids = set(candidate["by_sample"].get(sample_id, ()))
        matched = sum(candidate_to_gold.get(candidate_id) in gold_ids for candidate_id in candidate_ids)
        add_counts(
            sample_id,
            "atomic_claim",
            true_positive=matched,
            false_positive=len(candidate_ids) - matched,
            false_negative=len(gold_ids) - matched,
        )

    coordinate_true_positive = 0
    coordinate_false_positive = 0
    coordinate_false_negative = 0
    coordinate_false_negatives_from_extraction = 0
    coordinate_false_negatives_from_mapping = 0
    coordinate_false_positives_from_mapping = 0
    coordinate_false_positives_from_unmatched_candidates = 0
    for candidate_id, gold_id in candidate_to_gold.items():
        sample_id = str(gold["assertions"][gold_id]["sample_id"])
        gold_coordinate = gold["coordinates"].get(gold_id)
        candidate_coordinate = candidate["coordinates"].get(candidate_id)
        if gold_coordinate is not None and candidate_coordinate == gold_coordinate:
            coordinate_true_positive += 1
            add_counts(sample_id, "state_coordinate", true_positive=1)
            continue
        if gold_coordinate is not None:
            coordinate_false_negative += 1
            coordinate_false_negatives_from_mapping += 1
            add_counts(sample_id, "state_coordinate", false_negative=1)
        if candidate_coordinate is not None:
            coordinate_false_positive += 1
            coordinate_false_positives_from_mapping += 1
            add_counts(sample_id, "state_coordinate", false_positive=1)
    for gold_id in match["unmatched_gold_ids"]:
        if gold_id not in gold["coordinates"]:
            continue
        coordinate_false_negative += 1
        coordinate_false_negatives_from_extraction += 1
        add_counts(str(gold["assertions"][gold_id]["sample_id"]), "state_coordinate", false_negative=1)
    for candidate_id in match["unmatched_candidate_ids"]:
        if candidate_id not in candidate["coordinates"]:
            continue
        coordinate_false_positive += 1
        coordinate_false_positives_from_unmatched_candidates += 1
        add_counts(
            str(candidate["assertions"][candidate_id]["sample_id"]),
            "state_coordinate",
            false_positive=1,
        )
    coordinate_metrics = _metrics_from_counts(
        coordinate_true_positive,
        coordinate_false_positive,
        coordinate_false_negative,
    )

    def mapped_candidate_assertion_id(assertion_id: object) -> str:
        value = str(assertion_id)
        return str(candidate_to_gold.get(value) or f"__unmatched_candidate__:{value}")

    def mapped_baseline_assertion_id(assertion_id: object) -> str:
        value = str(assertion_id)
        if baseline_match is None:
            return value if value in gold["atomic_ids"] else f"__unmatched_baseline__:{value}"
        return str(baseline_to_gold.get(value) or f"__unmatched_baseline__:{value}")

    predicted_edge_origins: dict[tuple[str, str], str] = {}
    for old_id_value, new_id_value in persisted_edges:
        old_id = str(old_id_value)
        new_id = str(new_id_value)
        mapped_edge = (mapped_candidate_assertion_id(old_id), mapped_candidate_assertion_id(new_id))
        origin = candidate["assertions"].get(old_id) or candidate["assertions"].get(new_id)
        predicted_edge_origins.setdefault(mapped_edge, str(origin["sample_id"]) if origin else "unknown")
    predicted_edges = set(predicted_edge_origins)
    edge_metrics = classification_metrics(gold["expected_edges"], predicted_edges)
    edge_false_negatives_from_extraction = 0
    edge_false_negatives_from_mapping = 0
    for edge in gold["expected_edges"]:
        sample_id = str(gold["assertions"][edge[0]]["sample_id"])
        if edge in predicted_edges:
            add_counts(sample_id, "supersede_edge", true_positive=1)
        else:
            if set(edge) <= match["matched_gold_ids"]:
                edge_false_negatives_from_mapping += 1
            else:
                edge_false_negatives_from_extraction += 1
            add_counts(sample_id, "supersede_edge", false_negative=1)
    for edge in predicted_edges - gold["expected_edges"]:
        add_counts(predicted_edge_origins[edge], "supersede_edge", false_positive=1)

    cross_coordinate_edges = 0
    for old_id, new_id in predicted_edges:
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

    baseline_current_ids = {
        mapped_baseline_assertion_id(value)
        for value in baseline_observations.get("current_injected_assertion_ids") or ()
    }
    candidate_current_ids = {
        mapped_candidate_assertion_id(value)
        for value in candidate_observations.get("current_injected_assertion_ids") or ()
    }
    baseline_rate = _stale_rate(gold["current_ids"], baseline_current_ids)
    candidate_rate = _stale_rate(gold["current_ids"], candidate_current_ids)
    historical_metrics = classification_metrics(
        gold["historical_ids"],
        {
            mapped_candidate_assertion_id(value)
            for value in candidate_observations.get("historical_retrieved_assertion_ids") or ()
        },
    )
    baseline_non_state = classification_metrics(
        gold["non_state_ids"],
        {mapped_baseline_assertion_id(value) for value in baseline_predictions.get("non_state_assertion_ids") or ()},
    )
    candidate_non_state = classification_metrics(
        gold["non_state_ids"],
        {mapped_candidate_assertion_id(value) for value in candidate["non_state_ids"]},
    )
    consistency = _three_run_consistency([projection["coordinate_occurrences"] for projection in candidate_projections])
    claim_inflation = _inflation(baseline_claim_count, candidate["claim_count"])
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

    metric_names = ("atomic_claim", "state_coordinate", "supersede_edge")
    metadata = {sample_id: dict(value) for sample_id, value in gold["sample_metadata"].items()}
    for sample_id, value in corpus.items():
        metadata[sample_id] = {"category": str(value["category"]), "subtype": str(value["subtype"])}

    def grouped_breakdown(group_field: str) -> dict[str, Any]:
        grouped: dict[str, dict[str, Counter[str]]] = {}
        for sample_id, per_metric in sample_counts.items():
            sample_metadata = metadata.get(sample_id, {"category": "unknown", "subtype": "unknown"})
            category = sample_metadata["category"]
            group = category if group_field == "category" else f"{category}/{sample_metadata['subtype']}"
            group_metrics = grouped.setdefault(group, {})
            for metric in metric_names:
                target = group_metrics.setdefault(metric, Counter())
                target.update(per_metric.get(metric, {}))
        return {
            group: {
                metric: _metrics_from_counts(
                    counts["true_positive"],
                    counts["false_positive"],
                    counts["false_negative"],
                )
                for metric, counts in metrics_by_name.items()
            }
            for group, metrics_by_name in sorted(grouped.items())
        }

    threshold_satisfiability = check_threshold_satisfiability(
        gold_atomic_count=len(gold["atomic_ids"]),
        gold_coordinate_count=len(gold["coordinates"]),
        gold_edge_count=len(gold["expected_edges"]),
        historical_assertion_count=len(gold["historical_ids"]),
        baseline_claim_count=baseline_claim_count,
    )
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
        "breakdown": {
            "by_category": grouped_breakdown("category"),
            "by_subtype": grouped_breakdown("subtype"),
        },
        "mapping_diagnostics": {
            "matched_assertions": len(candidate_to_gold),
            "identity_matches": match["identity_matches"],
            "layout_remapped_matches": match["layout_remapped_matches"],
            "unmatched_candidates": len(match["unmatched_candidate_ids"]),
            "unmatched_gold": len(match["unmatched_gold_ids"]),
            "gold_zero_false_positives": match["gold_zero_false_positives"],
            "semantic_rejections": match["semantic_rejections"],
            "corpus_samples_available": len(corpus),
            "coordinate_false_negatives_from_extraction": coordinate_false_negatives_from_extraction,
            "coordinate_false_negatives_from_mapping": coordinate_false_negatives_from_mapping,
            "coordinate_false_positives_from_mapping": coordinate_false_positives_from_mapping,
            "coordinate_false_positives_from_unmatched_candidates": (
                coordinate_false_positives_from_unmatched_candidates
            ),
            "edge_false_negatives_from_extraction": edge_false_negatives_from_extraction,
            "edge_false_negatives_from_mapping": edge_false_negatives_from_mapping,
        },
        "thresholds": {name: dict(value) for name, value in THRESHOLDS.items()},
        "threshold_satisfiability": threshold_satisfiability,
        "checks": checks,
        "passed": all(check["passed"] for check in checks.values()) and threshold_satisfiability["satisfiable"],
    }


def score_protocol_file(
    gold_path: str | Path,
    *,
    baseline_predictions: Mapping[str, Any],
    candidate_runs: Sequence[Sequence[Mapping[str, Any]]],
    persisted_edges: Collection[tuple[str, str]],
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
    baseline_run: Sequence[Mapping[str, Any]] | None = None,
    corpus_path: str | Path | None = None,
) -> dict[str, Any]:
    """Consume dev or sealed gold internally and return aggregate metrics only."""

    if corpus_path is None:
        raise ValueError("corpus_path is required for semantic protocol scoring")
    corpus_records: list[Mapping[str, Any]] = []
    for line_index, line in enumerate(Path(corpus_path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError(f"corpus JSONL line {line_index} must be an object")
        corpus_records.append(value)
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
        baseline_run=baseline_run,
        corpus_records=corpus_records,
    )
