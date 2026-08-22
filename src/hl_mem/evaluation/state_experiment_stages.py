"""Four private pure stages behind the state experiment scorer facade."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from hl_mem.evaluation.state_experiment_projection import (
    _corpus_projection,
    _gold_projection,
    _match_assertions,
    project_run,
)
from hl_mem.evaluation.state_experiment_thresholds import (
    THRESHOLDS,
    check_threshold_satisfiability,
    threshold_passes,
)
from hl_mem.evaluation.state_protocol import CountMetrics


def _project_inputs(
    gold_records: Sequence[Mapping[str, Any]],
    *,
    baseline_predictions: Mapping[str, Any],
    candidate_runs: Sequence[Sequence[Mapping[str, Any]]],
    baseline_projection: Mapping[str, Any] | None,
    corpus_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(candidate_runs) != 3:
        raise ValueError("three candidate runs are required for coordinate consistency")
    gold = _gold_projection(gold_records)
    if not corpus_records:
        raise ValueError("corpus_records are required for semantic protocol scoring")
    corpus = _corpus_projection(corpus_records)
    candidate_projections = [project_run(run) for run in candidate_runs]
    candidate = candidate_projections[0]
    projection_keys = {
        "atomic_ids",
        "assertions",
        "by_sample",
        "coordinates",
        "non_state_ids",
        "coordinate_occurrences",
        "claim_count",
    }
    if baseline_projection is not None and (
        not isinstance(baseline_projection, Mapping) or not projection_keys.issubset(baseline_projection)
    ):
        raise ValueError("baseline_projection must be produced by project_run")
    declared_baseline_count = baseline_predictions.get("claim_count")
    if (
        isinstance(declared_baseline_count, bool)
        or not isinstance(declared_baseline_count, int)
        or declared_baseline_count < 0
    ):
        raise ValueError("baseline claim_count must be a non-negative integer")
    if baseline_projection is not None and declared_baseline_count != baseline_projection["claim_count"]:
        raise ValueError("baseline claim_count does not match baseline_projection")
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
    baseline_match = _match_assertions(gold, baseline_projection, corpus) if baseline_projection is not None else None
    return {
        "gold": gold,
        "corpus": corpus,
        "candidate_projections": candidate_projections,
        "candidate": candidate,
        "baseline_projection": baseline_projection,
        "baseline_claim_count": baseline_claim_count,
        "match": match,
        "baseline_match": baseline_match,
    }


def _add_counts(
    sample_counts: dict[str, dict[str, Counter[str]]],
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


def _mapped_candidate_id(projected: Mapping[str, Any], assertion_id: object) -> str:
    value = str(assertion_id)
    candidate_to_gold = projected["match"]["candidate_to_gold"]
    return str(candidate_to_gold.get(value) or f"__unmatched_candidate__:{value}")


def _mapped_baseline_id(projected: Mapping[str, Any], assertion_id: object) -> str:
    value = str(assertion_id)
    baseline_match = projected["baseline_match"]
    if baseline_match is None:
        return value if value in projected["gold"]["atomic_ids"] else f"__unmatched_baseline__:{value}"
    return str(baseline_match["candidate_to_gold"].get(value) or f"__unmatched_baseline__:{value}")


def _match_ledger(
    projected: Mapping[str, Any],
    persisted_edges: Collection[tuple[str, str]],
) -> dict[str, Any]:
    gold = projected["gold"]
    corpus = projected["corpus"]
    candidate = projected["candidate"]
    match = projected["match"]
    candidate_to_gold = match["candidate_to_gold"]
    atomic_metrics = CountMetrics(
        len(candidate_to_gold),
        len(match["unmatched_candidate_ids"]),
        len(match["unmatched_gold_ids"]),
    ).as_dict()
    sample_counts: dict[str, dict[str, Counter[str]]] = {}
    for sample_id in sorted(set(gold["by_sample"]) | set(candidate["by_sample"])):
        gold_ids = set(gold["by_sample"].get(sample_id, ()))
        candidate_ids = set(candidate["by_sample"].get(sample_id, ()))
        matched = sum(candidate_to_gold.get(candidate_id) in gold_ids for candidate_id in candidate_ids)
        _add_counts(
            sample_counts,
            sample_id,
            "atomic_claim",
            true_positive=matched,
            false_positive=len(candidate_ids) - matched,
            false_negative=len(gold_ids) - matched,
        )

    coordinate_counts = Counter[str]()
    coordinate_origins = Counter[str]()
    for candidate_id, gold_id in candidate_to_gold.items():
        sample_id = str(gold["assertions"][gold_id]["sample_id"])
        gold_coordinate = gold["coordinates"].get(gold_id)
        candidate_coordinate = candidate["coordinates"].get(candidate_id)
        if gold_coordinate is not None and candidate_coordinate == gold_coordinate:
            coordinate_counts["true_positive"] += 1
            _add_counts(sample_counts, sample_id, "state_coordinate", true_positive=1)
            continue
        if gold_coordinate is not None:
            coordinate_counts["false_negative"] += 1
            coordinate_origins["false_negatives_from_mapping"] += 1
            _add_counts(sample_counts, sample_id, "state_coordinate", false_negative=1)
        if candidate_coordinate is not None:
            coordinate_counts["false_positive"] += 1
            coordinate_origins["false_positives_from_mapping"] += 1
            _add_counts(sample_counts, sample_id, "state_coordinate", false_positive=1)
    for gold_id in match["unmatched_gold_ids"]:
        if gold_id not in gold["coordinates"]:
            continue
        coordinate_counts["false_negative"] += 1
        coordinate_origins["false_negatives_from_extraction"] += 1
        _add_counts(
            sample_counts,
            str(gold["assertions"][gold_id]["sample_id"]),
            "state_coordinate",
            false_negative=1,
        )
    for candidate_id in match["unmatched_candidate_ids"]:
        if candidate_id not in candidate["coordinates"]:
            continue
        coordinate_counts["false_positive"] += 1
        coordinate_origins["false_positives_from_unmatched_candidates"] += 1
        _add_counts(
            sample_counts,
            str(candidate["assertions"][candidate_id]["sample_id"]),
            "state_coordinate",
            false_positive=1,
        )
    coordinate_metrics = CountMetrics(
        coordinate_counts["true_positive"],
        coordinate_counts["false_positive"],
        coordinate_counts["false_negative"],
    ).as_dict()

    predicted_edge_origins: dict[tuple[str, str], str] = {}
    for old_id_value, new_id_value in persisted_edges:
        old_id = str(old_id_value)
        new_id = str(new_id_value)
        mapped_edge = (_mapped_candidate_id(projected, old_id), _mapped_candidate_id(projected, new_id))
        origin = candidate["assertions"].get(old_id) or candidate["assertions"].get(new_id)
        predicted_edge_origins.setdefault(mapped_edge, str(origin["sample_id"]) if origin else "unknown")
    predicted_edges = set(predicted_edge_origins)
    edge_metrics = CountMetrics.classify(gold["expected_edges"], predicted_edges).as_dict()
    edge_origins = Counter[str]()
    for edge in gold["expected_edges"]:
        sample_id = str(gold["assertions"][edge[0]]["sample_id"])
        if edge in predicted_edges:
            _add_counts(sample_counts, sample_id, "supersede_edge", true_positive=1)
        else:
            origin = (
                "false_negatives_from_mapping"
                if set(edge) <= match["matched_gold_ids"]
                else "false_negatives_from_extraction"
            )
            edge_origins[origin] += 1
            _add_counts(sample_counts, sample_id, "supersede_edge", false_negative=1)
    for edge in predicted_edges - gold["expected_edges"]:
        _add_counts(sample_counts, predicted_edge_origins[edge], "supersede_edge", false_positive=1)

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
    return {
        "atomic_metrics": atomic_metrics,
        "coordinate_metrics": coordinate_metrics,
        "edge_metrics": edge_metrics,
        "cross_coordinate_edges": cross_coordinate_edges,
        "sample_counts": sample_counts,
        "mapping_diagnostics": {
            "matched_assertions": len(candidate_to_gold),
            "identity_matches": match["identity_matches"],
            "layout_remapped_matches": match["layout_remapped_matches"],
            "unmatched_candidates": len(match["unmatched_candidate_ids"]),
            "unmatched_gold": len(match["unmatched_gold_ids"]),
            "gold_zero_false_positives": match["gold_zero_false_positives"],
            "semantic_rejections": match["semantic_rejections"],
            "corpus_samples_available": len(corpus),
            "coordinate_false_negatives_from_extraction": coordinate_origins["false_negatives_from_extraction"],
            "coordinate_false_negatives_from_mapping": coordinate_origins["false_negatives_from_mapping"],
            "coordinate_false_positives_from_mapping": coordinate_origins["false_positives_from_mapping"],
            "coordinate_false_positives_from_unmatched_candidates": coordinate_origins[
                "false_positives_from_unmatched_candidates"
            ],
            "edge_false_negatives_from_extraction": edge_origins["false_negatives_from_extraction"],
            "edge_false_negatives_from_mapping": edge_origins["false_negatives_from_mapping"],
        },
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


def _legacy_inflation(baseline: int, candidate: int) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else float("inf")
    return (candidate - baseline) / baseline


def _gold_normalized_unmatched_rate(gold_count: int, unmatched_candidate_count: int) -> float:
    if gold_count == 0:
        return 0.0 if unmatched_candidate_count == 0 else float("inf")
    return unmatched_candidate_count / gold_count


def _three_run_consistency(projections: Sequence[Sequence[tuple[str, str]]]) -> float:
    counters = [Counter(projection) for projection in projections]
    denominator = max((sum(counter.values()) for counter in counters), default=0)
    if denominator == 0:
        return 1.0
    reference = counters[0]
    consistent = sum(min(counter[coordinate] for counter in counters) for coordinate in reference)
    return consistent / denominator


def _grouped_breakdown(
    projected: Mapping[str, Any],
    ledger: Mapping[str, Any],
    group_field: str,
) -> dict[str, Any]:
    metric_names = ("atomic_claim", "state_coordinate", "supersede_edge")
    metadata = {sample_id: dict(value) for sample_id, value in projected["gold"]["sample_metadata"].items()}
    for sample_id, value in projected["corpus"].items():
        metadata[sample_id] = {"category": str(value["category"]), "subtype": str(value["subtype"])}
    grouped: dict[str, dict[str, Counter[str]]] = {}
    for sample_id, per_metric in ledger["sample_counts"].items():
        sample_metadata = metadata.get(sample_id, {"category": "unknown", "subtype": "unknown"})
        category = sample_metadata["category"]
        group = category if group_field == "category" else f"{category}/{sample_metadata['subtype']}"
        group_metrics = grouped.setdefault(group, {})
        for metric in metric_names:
            group_metrics.setdefault(metric, Counter()).update(per_metric.get(metric, {}))
    return {
        group: {
            metric: CountMetrics(
                counts["true_positive"],
                counts["false_positive"],
                counts["false_negative"],
            ).as_dict()
            for metric, counts in metrics_by_name.items()
        }
        for group, metrics_by_name in sorted(grouped.items())
    }


def _derive_metrics(
    projected: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    baseline_predictions: Mapping[str, Any],
    baseline_observations: Mapping[str, Any],
    candidate_observations: Mapping[str, Any],
) -> dict[str, Any]:
    gold = projected["gold"]
    candidate = projected["candidate"]
    baseline_current_ids = {
        _mapped_baseline_id(projected, value)
        for value in baseline_observations.get("current_injected_assertion_ids") or ()
    }
    candidate_current_ids = {
        _mapped_candidate_id(projected, value)
        for value in candidate_observations.get("current_injected_assertion_ids") or ()
    }
    baseline_rate = _stale_rate(gold["current_ids"], baseline_current_ids)
    candidate_rate = _stale_rate(gold["current_ids"], candidate_current_ids)
    historical_metrics = CountMetrics.classify(
        gold["historical_ids"],
        {
            _mapped_candidate_id(projected, value)
            for value in candidate_observations.get("historical_retrieved_assertion_ids") or ()
        },
    ).as_dict()
    baseline_non_state = CountMetrics.classify(
        gold["non_state_ids"],
        {_mapped_baseline_id(projected, value) for value in baseline_predictions.get("non_state_assertion_ids") or ()},
    ).as_dict()
    candidate_non_state = CountMetrics.classify(
        gold["non_state_ids"],
        {_mapped_candidate_id(projected, value) for value in candidate["non_state_ids"]},
    ).as_dict()
    consistency = _three_run_consistency(
        [projection["coordinate_occurrences"] for projection in projected["candidate_projections"]]
    )
    claim_inflation = _gold_normalized_unmatched_rate(
        len(gold["atomic_ids"]),
        len(projected["match"]["unmatched_candidate_ids"]),
    )
    legacy_inflation = _legacy_inflation(projected["baseline_claim_count"], candidate["claim_count"])
    non_state_f1_drop = float(baseline_non_state["f1"]) - float(candidate_non_state["f1"])
    stale_reduction = _reduction(baseline_rate, candidate_rate)
    return {
        "metrics": {
            "state_coordinate": ledger["coordinate_metrics"],
            "atomic_claim": ledger["atomic_metrics"],
            "supersede_edge": ledger["edge_metrics"],
            "counterexample_cross_coordinate_supersede": ledger["cross_coordinate_edges"],
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
            "inflation_legacy_vs_arm_a": legacy_inflation,
            "three_run_coordinate_consistency": consistency,
        },
        "breakdown": {
            "by_category": _grouped_breakdown(projected, ledger, "category"),
            "by_subtype": _grouped_breakdown(projected, ledger, "subtype"),
        },
    }


def _evaluate_gates(
    projected: Mapping[str, Any],
    derived: Mapping[str, Any],
) -> dict[str, Any]:
    gold = projected["gold"]
    metrics = derived["metrics"]
    satisfiability = check_threshold_satisfiability(
        gold_atomic_count=len(gold["atomic_ids"]),
        gold_coordinate_count=len(gold["coordinates"]),
        gold_edge_count=len(gold["expected_edges"]),
        historical_assertion_count=len(gold["historical_ids"]),
        baseline_claim_count=projected["baseline_claim_count"],
    )
    actuals: dict[str, float | int] = {
        "state_coordinate_precision": float(metrics["state_coordinate"]["precision"]),
        "state_coordinate_recall": float(metrics["state_coordinate"]["recall"]),
        "atomic_claim_precision": float(metrics["atomic_claim"]["precision"]),
        "atomic_claim_recall": float(metrics["atomic_claim"]["recall"]),
        "supersede_edge_precision": float(metrics["supersede_edge"]["precision"]),
        "supersede_edge_recall": float(metrics["supersede_edge"]["recall"]),
        "counterexample_cross_coordinate_supersede": metrics["counterexample_cross_coordinate_supersede"],
        "stale_injection_reduction": metrics["current_state_stale_injection"]["reduction"],
        "stale_injection_absolute": metrics["current_state_stale_injection"]["candidate_rate"],
        "historical_old_snapshot_recall": float(metrics["historical_old_snapshot_recall"]),
        "non_state_f1_drop": metrics["non_state_extraction"]["f1_drop"],
        "claim_inflation": metrics["claim_inflation"],
        "three_run_coordinate_consistency": metrics["three_run_coordinate_consistency"],
    }
    checks = {
        name: {
            "actual": actuals[name],
            **threshold,
            "passed": threshold_passes(str(threshold["operator"]), actuals[name], threshold["target"]),
        }
        for name, threshold in THRESHOLDS.items()
    }
    return {
        "thresholds": {name: dict(value) for name, value in THRESHOLDS.items()},
        "threshold_satisfiability": satisfiability,
        "checks": checks,
        "passed": all(check["passed"] for check in checks.values()) and satisfiability["satisfiable"],
    }
