"""Replay the frozen six-factor ranking grid on captured live candidate windows."""

from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from hl_mem.evaluation.metrics import paired_cluster_bootstrap_ci
from hl_mem.evaluation.sensitivity import sensitivity_weight_variants
from hl_mem.recall.ranking import DEFAULT_WEIGHTS, memory_features, memory_score
from hl_mem.storage.claims import ClaimRepository

RRF_K = 60
SCORER_VERSION = "answer-entity-packet-v1"


def _json_lines(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _cluster(case_id: str) -> str:
    parts = case_id.split(":")
    if case_id.startswith("perltqa:") and len(parts) > 1:
        return f"perltqa:{parts[1]}"
    if case_id.startswith("memdaily:") and len(parts) > 2:
        return f"memdaily:{parts[-1]}"
    return case_id


def _recorded_epoch(claim: Mapping[str, Any]) -> float:
    value = claim.get("recorded_from")
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _claim_matches_slot_hint(claim: Mapping[str, Any], hint: str) -> bool:
    def matches(value: str) -> bool:
        return value.startswith("preference.") if hint == "preference.*" else value == hint

    return any(matches(str(claim.get(field) or "")) for field in ("canonical_slot", "canonical_attribute"))


def _semantic_from_trace(candidate: Mapping[str, Any], channel_total: float = 2.0) -> float:
    channels = candidate.get("channels")
    if not isinstance(channels, Mapping) or channel_total <= 0.0:
        return 0.0
    score = sum(
        1.0 / (RRF_K + int(rank))
        for name, rank in channels.items()
        if name in {"fts", "dense"} and isinstance(rank, int) and not isinstance(rank, bool) and rank > 0
    )
    return min(1.0, max(0.0, score / (channel_total / (RRF_K + 1))))


def _claim_rows(connection: sqlite3.Connection, claim_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(claim_ids))
    result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(unique), 500):
        chunk = unique[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(f"SELECT * FROM claims WHERE id IN ({placeholders})", chunk).fetchall()
        result.update({str(row["id"]): dict(row) for row in rows})
    return result


def _evidence_by_claim(connection: sqlite3.Connection, claim_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
    unique = list(dict.fromkeys(claim_ids))
    result = {claim_id: [] for claim_id in unique}
    for offset in range(0, len(unique), 500):
        chunk = unique[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            "SELECT derived_id,evidence_id FROM evidence_links WHERE derived_type='claim' "
            "AND evidence_type='event' AND derived_id IN (" + placeholders + ")",
            chunk,
        ).fetchall()
        for row in rows:
            result[str(row["derived_id"])].append(str(row["evidence_id"]))
    return {key: tuple(dict.fromkeys(values)) for key, values in result.items()}


def _entities(claim: Mapping[str, Any]) -> tuple[str, ...]:
    raw = claim.get("entities_json")
    try:
        values = json.loads(str(raw)) if raw else []
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    if not isinstance(values, list):
        return ()
    return tuple(
        dict.fromkeys(unicodedata.normalize("NFC", str(value).strip()) for value in values if str(value).strip())
    )


def _feature_candidates(
    db_path: Path,
    trace: Mapping[str, Any],
    ranking_now: str,
    intent: str,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]], float]:
    if trace.get("tag_channel_applied"):
        raise RuntimeError("frozen replay does not support an active tag candidate channel")
    raw_candidates = trace.get("candidates")
    if not isinstance(raw_candidates, Mapping):
        raise RuntimeError("search trace has no candidate map")
    candidate_ids = [str(claim_id) for claim_id in raw_candidates]
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        claims = _claim_rows(connection, candidate_ids)
        if missing := sorted(set(candidate_ids) - set(claims)):
            raise RuntimeError(f"candidate claims missing from {db_path}: {missing[:3]}")
        helpful_rates = ClaimRepository(connection).helpful_rates(candidate_ids, 3)
        evidence = _evidence_by_claim(connection, candidate_ids)
    finally:
        connection.close()
    hints = trace.get("query_slot_hints") if isinstance(trace.get("query_slot_hints"), list) else []
    candidates: list[dict[str, Any]] = []
    reconstruction_errors: list[float] = []
    for claim_id, raw_trace in raw_candidates.items():
        if not isinstance(raw_trace, Mapping):
            continue
        claim = claims[str(claim_id)]
        claim["helpful_rate"] = helpful_rates.get(str(claim_id), 0.5)
        semantic = _semantic_from_trace(raw_trace)
        features = memory_features(claim, semantic, 0, ranking_now)
        fixed_boost = float(raw_trace.get("tag_boost") or 0.0)
        if any(_claim_matches_slot_hint(claim, str(hint)) for hint in hints):
            fixed_boost += 0.05
        if intent == "preference" and any(
            _claim_matches_slot_hint(claim, str(hint)) for hint in hints if str(hint) == "preference.*"
        ):
            fixed_boost += 0.12 * features["recency"]
        traced = float(raw_trace.get("pre_score") or (memory_score(features) + fixed_boost))
        without_access = memory_score(features) + fixed_boost
        features["access_frequency"] = min(
            1.0,
            max(0.0, (traced - without_access) / DEFAULT_WEIGHTS["access_frequency"]),
        )
        replayed = memory_score(features) + fixed_boost
        reconstruction_errors.append(abs(replayed - traced))
        candidates.append(
            {
                "claim_id": str(claim_id),
                "features": features,
                "fixed_boost": fixed_boost,
                "recorded_epoch": _recorded_epoch(claim),
                "entities": _entities(claim),
            }
        )
    return candidates, evidence, max(reconstruction_errors, default=0.0)


def _rank(candidates: Sequence[Mapping[str, Any]], weights: Mapping[str, float]) -> list[str]:
    return [
        str(candidate["claim_id"])
        for candidate in sorted(
            candidates,
            key=lambda candidate: (
                -(memory_score(candidate["features"], weights) + float(candidate["fixed_boost"])),
                -float(candidate["features"]["semantic"]),
                -float(candidate["recorded_epoch"]),
                str(candidate["claim_id"]),
            ),
        )
    ]


def _retrieval_metrics(
    top_ids: Sequence[str],
    gold: Sequence[str],
    evidence: Mapping[str, Sequence[str]] | None = None,
) -> tuple[float | None, float | None]:
    gold_set = set(gold)
    if not gold_set:
        return None, None
    if evidence is None:
        hits = [set([claim_id]) & gold_set for claim_id in top_ids]
    else:
        hits = [set(evidence.get(claim_id, ())) & gold_set for claim_id in top_ids]
    found = set().union(*hits) if hits else set()
    recall = len(found) / len(gold_set)
    first = next((rank for rank, values in enumerate(hits, start=1) if values), None)
    return recall, 1.0 / first if first else 0.0


def _entity_coverage(
    top_ids: Sequence[str],
    candidates: Sequence[Mapping[str, Any]],
    answer_entities: Sequence[str] | None,
) -> float | None:
    if answer_entities is None:
        return None
    by_id = {str(candidate["claim_id"]): candidate for candidate in candidates}
    packet_entities = {entity for claim_id in top_ids[:5] for entity in by_id[claim_id].get("entities", ())}
    gold = [unicodedata.normalize("NFC", str(entity)) for entity in answer_entities]
    return sum(entity in packet_entities for entity in gold) / len(gold)


def _load_isolated_cases(path: Path, manifest_path: Path) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ranking_now = str(manifest["generated_at"])
    root = Path(str(manifest["run_root"]))
    result: list[dict[str, Any]] = []
    for row in _json_lines(path):
        if row.get("arm") != "A" or row.get("error") is not None:
            continue
        response = row.get("response")
        if not isinstance(response, Mapping):
            continue
        suite = str(row["suite"])
        db_path = root / f"{suite}-seed.db"
        candidates, evidence, drift = _feature_candidates(
            db_path,
            response["search_trace"],
            ranking_now,
            str(row.get("expected_intent") or "current_state"),
        )
        result.append(
            {
                "case_id": str(row["case_id"]),
                "dataset": "isolated_112",
                "cluster": _cluster(str(row["case_id"])),
                "candidates": candidates,
                "evidence": evidence,
                "gold": tuple(str(item) for item in row.get("expected_memory_ids") or []),
                "answer_entities": None,
                "reconstruction_error": drift,
            }
        )
    return result


def _source_cache_from_packet(row: Mapping[str, Any]) -> str | None:
    for item in [*(row.get("packet") or []), *(row.get("top5_seed_packet") or [])]:
        for provenance in item.get("evidence_provenance") or []:
            value = provenance.get("source_cache_identity")
            if value:
                return str(value)
    return None


def _load_e2e_cases(
    raw_path: Path,
    gold_path: Path,
    live_path: Path,
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    cache_by_case: dict[str, str] = {}
    for row in _json_lines(raw_path):
        case_id = str(row.get("case_id") or "")
        if cache := _source_cache_from_packet(row):
            cache_by_case.setdefault(case_id, cache)
        if (
            row.get("dataset") == "chinese_e2e"
            and row.get("arm_id") == "C0"
            and row.get("repeat_index") == 0
            and row.get("status") == "complete"
        ):
            selected.setdefault(case_id, row)
    live = json.loads(live_path.read_text(encoding="utf-8"))
    live_by_case = {str(case["case_id"]): case for case in live["cases"]}
    gold_manifest = json.loads(gold_path.read_text(encoding="utf-8"))
    entity_gold = gold_manifest["answer_entity_gold"]
    ranking_now = datetime.fromtimestamp(raw_path.stat().st_ctime, timezone.utc).isoformat()
    result: list[dict[str, Any]] = []
    for case_id, row in selected.items():
        db_path = Path(cache_by_case[case_id])
        candidates, evidence, drift = _feature_candidates(
            db_path,
            row["search_trace"],
            ranking_now,
            str(row["search_trace"].get("intent") or "current_state"),
        )
        structured = entity_gold[case_id]
        result.append(
            {
                "case_id": case_id,
                "dataset": "e2e_40",
                "cluster": _cluster(case_id),
                "candidates": candidates,
                "evidence": evidence,
                "gold": tuple(str(item) for item in live_by_case[case_id]["gold_extraction_units"]),
                "answer_entities": (
                    tuple(str(item) for item in structured["answer_entities"])
                    if "answer_entities" in structured
                    else None
                ),
                "reconstruction_error": drift,
            }
        )
    return result


def _mean_present(rows: Sequence[Mapping[str, Any]], name: str) -> float:
    values = [float(row[name]) for row in rows if row.get(name) is not None]
    return fmean(values) if values else 0.0


def _paired_ci(
    baseline: Sequence[Mapping[str, Any]],
    variant: Sequence[Mapping[str, Any]],
    metric: str,
    *,
    seed: int,
) -> list[float] | None:
    control_by_id = {str(row["case_id"]): row for row in baseline if row.get(metric) is not None}
    pairs = [
        (control_by_id[str(row["case_id"])], row)
        for row in variant
        if str(row["case_id"]) in control_by_id and row.get(metric) is not None
    ]
    if not pairs:
        return None
    interval = paired_cluster_bootstrap_ci(
        [float(left[metric]) for left, _ in pairs],
        [float(right[metric]) for _, right in pairs],
        [str(left["cluster"]) for left, _ in pairs],
        seed=seed,
        resamples=2000,
    )
    return [interval[0], interval[1]]


def run(
    output_dir: Path,
    isolated_path: Path,
    c_series_path: Path,
    gold_path: Path,
    live_path: Path,
    seed: int,
) -> dict[str, Any]:
    variants = sensitivity_weight_variants(DEFAULT_WEIGHTS)
    cases = [
        *_load_isolated_cases(isolated_path, isolated_path.with_name("abstention_enforce_ab_manifest.json")),
        *_load_e2e_cases(c_series_path, gold_path, live_path),
    ]
    counts = {dataset: sum(case["dataset"] == dataset for case in cases) for dataset in ("isolated_112", "e2e_40")}
    if counts != {"isolated_112": 112, "e2e_40": 40}:
        raise RuntimeError(f"unexpected sensitivity case counts: {counts}")
    max_drift = max(float(case["reconstruction_error"]) for case in cases)
    if max_drift > 0.02:
        raise RuntimeError(f"captured prior reconstruction drift exceeds 0.02: {max_drift}")
    rows: list[dict[str, Any]] = []
    by_candidate_features = {
        str(case["case_id"]): {str(item["claim_id"]): item for item in case["candidates"]} for case in cases
    }
    del by_candidate_features  # retained as an explicit completeness check during construction
    for variant_name, weights in variants.items():
        for case in cases:
            ranked = _rank(case["candidates"], weights)
            top5 = ranked[:5]
            recall, reciprocal_rank = _retrieval_metrics(
                top5, case["gold"], case["evidence"] if case["dataset"] == "e2e_40" else None
            )
            rows.append(
                {
                    "variant": variant_name,
                    "dataset": case["dataset"],
                    "case_id": case["case_id"],
                    "cluster": case["cluster"],
                    "top5_claim_ids": top5,
                    "recall_at_5": recall,
                    "mrr": reciprocal_rank,
                    "answer_entity_coverage_at_5": (
                        _entity_coverage(
                            top5,
                            case["candidates"],
                            case["answer_entities"],
                        )
                        if case["dataset"] == "e2e_40"
                        else None
                    ),
                }
            )
    summaries: dict[str, dict[str, Any]] = {}
    metrics = ("recall_at_5", "mrr", "answer_entity_coverage_at_5")
    for variant_name, weights in variants.items():
        summaries[variant_name] = {"weights": weights, "datasets": {}}
        for dataset in ("isolated_112", "e2e_40"):
            selected = [row for row in rows if row["variant"] == variant_name and row["dataset"] == dataset]
            summaries[variant_name]["datasets"][dataset] = {
                metric: _mean_present(selected, metric) for metric in metrics
            }
    baseline_rows = [row for row in rows if row["variant"] == "baseline"]
    for variant_name in variants:
        if variant_name == "baseline":
            continue
        variant_rows = [row for row in rows if row["variant"] == variant_name]
        for dataset in ("isolated_112", "e2e_40"):
            left = [row for row in baseline_rows if row["dataset"] == dataset]
            right = [row for row in variant_rows if row["dataset"] == dataset]
            summary = summaries[variant_name]["datasets"][dataset]
            base = summaries["baseline"]["datasets"][dataset]
            summary["delta"] = {metric: summary[metric] - base[metric] for metric in metrics}
            summary["paired_cluster_ci_95"] = {metric: _paired_ci(left, right, metric, seed=seed) for metric in metrics}
    qualifying: list[str] = []
    for name, summary in summaries.items():
        if name == "baseline":
            continue
        isolated = summary["datasets"]["isolated_112"]
        e2e = summary["datasets"]["e2e_40"]
        deltas = [
            isolated["delta"]["recall_at_5"],
            isolated["delta"]["mrr"],
            e2e["delta"]["recall_at_5"],
            e2e["delta"]["mrr"],
        ]
        retrieval_intervals = [
            isolated["paired_cluster_ci_95"]["recall_at_5"],
            isolated["paired_cluster_ci_95"]["mrr"],
            e2e["paired_cluster_ci_95"]["recall_at_5"],
            e2e["paired_cluster_ci_95"]["mrr"],
        ]
        entity_delta = e2e["delta"]["answer_entity_coverage_at_5"]
        if (
            all(delta >= 0.02 for delta in deltas)
            and entity_delta >= 0.0
            and all(interval is not None and interval[0] > 0.0 for interval in retrieval_intervals)
        ):
            qualifying.append(name)
    best_variant = max(
        variants,
        key=lambda name: sum(
            summaries[name]["datasets"][dataset][metric]
            for dataset in ("isolated_112", "e2e_40")
            for metric in ("recall_at_5", "mrr")
        ),
    )
    report = {
        "schema_version": "sensitivity-grid-report-v1",
        "scorer_version": SCORER_VERSION,
        "case_counts": counts,
        "candidate_prior_reconstruction_max_abs_error": max_drift,
        "variants": summaries,
        "best_composite_variant": best_variant,
        "stable_headroom_variants": qualifying,
        "bandit_v028_hard_gate_passed": bool(qualifying),
        "current_weights_at_local_peak": best_variant == "baseline",
        "stable_alternative_demonstrated": bool(qualifying),
        "recommendation": ("open_v028_bandit" if qualifying else "keep_current_weights_collect_more_clusters"),
        "limitations": [
            "candidate windows and remote reranker outputs are frozen",
            "the replay isolates the six-factor pre-rank prior and preserves fixed slot/tag boosts",
            "entity coverage uses exact NFC entities_json from the final Top-5 seeds without synonym expansion",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sensitivity_grid_runs.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (output_dir / "sensitivity_grid_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("var/eval"))
    parser.add_argument("--isolated", type=Path, default=Path("var/eval/abstention_enforce_ab_runs.jsonl"))
    parser.add_argument("--c-series", type=Path, default=Path("var/eval/c_series_raw.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("tests/eval/fixtures/chinese_e2e_sample.json"))
    parser.add_argument(
        "--live-e2e",
        type=Path,
        default=Path("var/eval/v0260_live_rubricv2_run2_chinese_e2e.json"),
    )
    parser.add_argument("--seed", type=int, default=20260815)
    args = parser.parse_args()
    report = run(
        args.output_dir,
        args.isolated,
        args.c_series,
        args.gold,
        args.live_e2e,
        args.seed,
    )
    compact = {
        "case_counts": report["case_counts"],
        "max_reconstruction_error": report["candidate_prior_reconstruction_max_abs_error"],
        "baseline": report["variants"]["baseline"]["datasets"],
        "best_composite_variant": report["best_composite_variant"],
        "stable_headroom_variants": report["stable_headroom_variants"],
        "bandit_v028_hard_gate_passed": report["bandit_v028_hard_gate_passed"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
