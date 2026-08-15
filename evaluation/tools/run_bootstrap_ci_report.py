"""Generate versioned cluster/paired bootstrap CI examples from frozen live runs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from hl_mem.evaluation.metrics import cluster_bootstrap_ci, paired_cluster_bootstrap_ci

SCHEMA_VERSION = "bootstrap-ci-report-v1"
SCORER_VERSION = "answer-entity-packet-v1"


def _cluster(case_id: str) -> str:
    parts = case_id.split(":")
    if case_id.startswith("perltqa:") and len(parts) > 1:
        return f"perltqa:{parts[1]}"
    if case_id.startswith("memdaily:") and len(parts) > 2:
        return f"memdaily:{parts[-1]}"
    return case_id


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _source_cache_from_row(row: Mapping[str, Any]) -> str | None:
    for item in [*(row.get("packet") or []), *(row.get("top5_seed_packet") or [])]:
        for provenance in item.get("evidence_provenance") or []:
            if value := provenance.get("source_cache_identity"):
                return str(value)
    return None


def _cache_by_case(c_series_path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    with c_series_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            case_id = str(row.get("case_id") or "")
            if case_id not in result and (cache := _source_cache_from_row(row)):
                result[case_id] = Path(cache)
    return result


def _claim_entities(db_path: Path, claim_ids: Sequence[str]) -> set[str]:
    if not claim_ids:
        return set()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in claim_ids)
        rows = connection.execute(
            f"SELECT entities_json FROM claims WHERE id IN ({placeholders})", list(claim_ids)
        ).fetchall()
    finally:
        connection.close()
    entities: set[str] = set()
    for row in rows:
        try:
            values = json.loads(str(row["entities_json"])) if row["entities_json"] else []
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        if isinstance(values, list):
            entities.update(unicodedata.normalize("NFC", str(value).strip()) for value in values if str(value).strip())
    return entities


def _entity_coverage(
    case: Mapping[str, Any],
    cache: Path,
    gold: Mapping[str, Any],
) -> float | None:
    raw = gold.get("answer_entities")
    if not isinstance(raw, list):
        return None
    answer_entities = [unicodedata.normalize("NFC", str(item)) for item in raw]
    top_ids = [str(item["claim_id"]) for item in (case.get("retrieved") or [])[:5]]
    packet_entities = _claim_entities(cache, top_ids)
    return sum(entity in packet_entities for entity in answer_entities) / len(answer_entities)


def _interval(values: list[float], clusters: list[str], seed: int, resamples: int) -> list[float]:
    low, high = cluster_bootstrap_ci(values, clusters, seed=seed, resamples=resamples)
    return [low, high]


def _paired(
    control: Sequence[Mapping[str, Any]],
    treatment: Sequence[Mapping[str, Any]],
    metric: str,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    left = {str(row["case_id"]): row for row in control if row.get(metric) is not None}
    pairs = [
        (left[str(row["case_id"])], row)
        for row in treatment
        if str(row["case_id"]) in left and row.get(metric) is not None
    ]
    control_values = [float(first[metric]) for first, _ in pairs]
    treatment_values = [float(second[metric]) for _, second in pairs]
    clusters = [str(first["cluster"]) for first, _ in pairs]
    low, high = paired_cluster_bootstrap_ci(
        control_values,
        treatment_values,
        clusters,
        seed=seed,
        resamples=resamples,
    )
    return {
        "paired_cases": len(pairs),
        "control_mean": fmean(control_values),
        "treatment_mean": fmean(treatment_values),
        "delta": fmean(treatment_values) - fmean(control_values),
        "delta_ci_95": [low, high],
    }


def _arm_summary(rows: Sequence[Mapping[str, Any]], seed: int, resamples: int) -> dict[str, Any]:
    result: dict[str, Any] = {"cases": len(rows), "clusters": len({row["cluster"] for row in rows})}
    for metric in ("recall_at_5", "mrr", "accuracy", "answer_entity_coverage_at_5"):
        selected = [row for row in rows if row.get(metric) is not None]
        if not selected:
            result[metric] = None
            continue
        values = [float(row[metric]) for row in selected]
        clusters = [str(row["cluster"]) for row in selected]
        result[metric] = {
            "cases": len(values),
            "mean": fmean(values),
            "cluster_bootstrap_ci_95": _interval(values, clusters, seed, resamples),
        }
    return result


def _isolated_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    arms: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
    for row in _load_jsonl(path):
        arm = str(row["arm"])
        gold = list(row.get("expected_memory_ids") or [])
        response = row.get("response") or {}
        answerability = str(response.get("answerability") or row.get("answerability") or "")
        expected_answerable = bool(gold)
        predicted_answerable = answerability == "supported"
        arms[arm].append(
            {
                "case_id": str(row["case_id"]),
                "cluster": _cluster(str(row["case_id"])),
                "recall_at_5": float(row["gold_recall_at_5"]) if gold else None,
                "mrr": (1.0 / int(row["rank"])) if gold and row.get("rank") else (0.0 if gold else None),
                "accuracy": float(expected_answerable == predicted_answerable),
                "answer_entity_coverage_at_5": None,
            }
        )
    return arms


def _e2e_rows(
    path: Path,
    cache_by_case: Mapping[str, Path],
    entity_gold: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = []
    for case in payload["cases"]:
        case_id = str(case["case_id"])
        qa = case.get("qa")
        accuracy: float | None = None
        if isinstance(qa, Mapping):
            if qa.get("answer_correct") is not None:
                accuracy = float(qa["answer_correct"])
            elif qa.get("choice_correct") is not None:
                accuracy = float(bool(qa["choice_correct"]))
        result.append(
            {
                "case_id": case_id,
                "cluster": _cluster(case_id),
                "recall_at_5": float(case["retrieval"]["recall_at_5"]),
                "mrr": float(case["retrieval"]["mrr"]),
                "accuracy": accuracy,
                "answer_entity_coverage_at_5": _entity_coverage(
                    case,
                    cache_by_case[case_id],
                    entity_gold[case_id],
                ),
            }
        )
    return result


def run(
    output_dir: Path,
    isolated_path: Path,
    e2e_control_path: Path,
    e2e_treatment_path: Path,
    c_series_path: Path,
    gold_path: Path,
    seed: int,
    resamples: int,
) -> dict[str, Any]:
    isolated = _isolated_rows(isolated_path)
    caches = _cache_by_case(c_series_path)
    gold_manifest = json.loads(gold_path.read_text(encoding="utf-8"))
    entity_gold = gold_manifest["answer_entity_gold"]
    e2e_control = _e2e_rows(e2e_control_path, caches, entity_gold)
    e2e_treatment = _e2e_rows(e2e_treatment_path, caches, entity_gold)
    report = {
        "schema_version": SCHEMA_VERSION,
        "scorer_version": SCORER_VERSION,
        "method": {
            "seed": seed,
            "resamples": resamples,
            "confidence": 0.95,
            "cluster_unit": {"perltqa": "persona", "memdaily": "trajectory"},
            "paired_effect": "treatment_minus_control",
        },
        "isolated_112": {
            "arms": {
                "A_observe": _arm_summary(isolated["A"], seed, resamples),
                "B_enforce": _arm_summary(isolated["B"], seed, resamples),
            },
            "paired_delta_B_minus_A": {
                metric: _paired(isolated["A"], isolated["B"], metric, seed, resamples)
                for metric in ("recall_at_5", "mrr", "accuracy")
            },
        },
        "e2e_40": {
            "arms": {
                "live_run1": _arm_summary(e2e_control, seed, resamples),
                "live_run2": _arm_summary(e2e_treatment, seed, resamples),
            },
            "paired_delta_run2_minus_run1": {
                metric: _paired(e2e_control, e2e_treatment, metric, seed, resamples)
                for metric in (
                    "recall_at_5",
                    "mrr",
                    "accuracy",
                    "answer_entity_coverage_at_5",
                )
            },
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bootstrap_ci_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Paired cluster bootstrap CI 样例",
        "",
        f"schema: `{SCHEMA_VERSION}`；seed={seed}；resamples={resamples}；95% CI。",
        "",
        "| 数据集 | 指标 | 配对数 | 差值 | 95% CI |",
        "|---|---:|---:|---:|---:|",
    ]
    for dataset, key in (
        ("isolated_112", "paired_delta_B_minus_A"),
        ("e2e_40", "paired_delta_run2_minus_run1"),
    ):
        for metric, value in report[dataset][key].items():
            lines.append(
                f"| {dataset} | {metric} | {value['paired_cases']} | {value['delta']:.4f} | "
                f"[{value['delta_ci_95'][0]:.4f}, {value['delta_ci_95'][1]:.4f}] |"
            )
    (output_dir / "bootstrap_ci_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("var/eval"))
    parser.add_argument("--isolated", type=Path, default=Path("var/eval/abstention_enforce_ab_runs.jsonl"))
    parser.add_argument(
        "--e2e-control",
        type=Path,
        default=Path("var/eval/v0260_live_rubricv2_chinese_e2e.json"),
    )
    parser.add_argument(
        "--e2e-treatment",
        type=Path,
        default=Path("var/eval/v0260_live_rubricv2_run2_chinese_e2e.json"),
    )
    parser.add_argument("--c-series", type=Path, default=Path("var/eval/c_series_raw.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("tests/eval/fixtures/chinese_e2e_sample.json"))
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--resamples", type=int, default=2000)
    args = parser.parse_args()
    if args.resamples < 1:
        parser.error("--resamples must be positive")
    report = run(
        args.output_dir,
        args.isolated,
        args.e2e_control,
        args.e2e_treatment,
        args.c_series,
        args.gold,
        args.seed,
        args.resamples,
    )
    compact = {
        "isolated_paired": report["isolated_112"]["paired_delta_B_minus_A"],
        "e2e_paired": report["e2e_40"]["paired_delta_run2_minus_run1"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
