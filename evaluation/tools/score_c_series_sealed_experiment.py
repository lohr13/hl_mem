#!/usr/bin/env python
"""Offline answer-entity-packet-v1 scorer for the sealed C-series matrix."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools import run_c_series_sealed_experiment as sealed_runner  # noqa: E402
from evaluation.tools import score_c_series_relation_experiment as frozen  # noqa: E402
from hl_mem.evaluation.c_series import (  # noqa: E402
    sha256_file,
    write_json_atomic,
)
from tests.eval.relation_chain_holdout import (  # noqa: E402
    load_holdout_manifest,
    load_sealed_holdout,
)

ARMS = ("C0", "C4")
READERS = ("qwen", "glm")
REPEATS = 3
IMPLEMENTATION_VERSION = "c-series-sealed-matrix-v2"


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def assert_implementation_snapshot(prereg: Mapping[str, Any]) -> None:
    files = {
        "sealed_runner": ROOT / "evaluation" / "tools" / "run_c_series_sealed_experiment.py",
        "sealed_scorer": Path(__file__),
        "base_runner": ROOT / "evaluation" / "tools" / "run_c_series_relation_experiment.py",
        "base_scorer": ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
        "runtime": ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        "protocol": ROOT / "src" / "hl_mem" / "evaluation" / "c_series.py",
        "sealed_holdout_loader": ROOT / "tests" / "eval" / "relation_chain_holdout.py",
        "relation_discovery": ROOT / "src" / "hl_mem" / "workers" / "discover_relations.py",
    }
    expected = {
        "version": IMPLEMENTATION_VERSION,
        **{f"{name}_sha256": sha256_file(path) for name, path in files.items()},
    }
    if prereg.get("implementation_snapshot") != expected:
        raise RuntimeError("sealed scorer implementation snapshot drift")


def assert_holdout_suite(path: Path, expected_suite: str) -> None:
    actual_suite = load_holdout_manifest(path).suite_version
    if actual_suite != expected_suite:
        raise RuntimeError(f"sealed holdout suite mismatch: expected {expected_suite}, got {actual_suite}")


def assert_exact_matrix(raw_rows: Sequence[Mapping[str, Any]], inputs: Mapping[str, Any]) -> None:
    case_ids = {str(case["case_id"]) for case in inputs.get("cases") or []}
    expected = {
        (case_id, repeat, arm, reader)
        for case_id in case_ids
        for repeat in range(REPEATS)
        for arm in ARMS
        for reader in READERS
    }
    actual = {
        (str(row["case_id"]), int(row["repeat_index"]), str(row["arm_id"]), str(row["reader_id"])) for row in raw_rows
    }
    if actual != expected or len(raw_rows) != len(expected):
        raise RuntimeError(
            f"sealed raw matrix keys differ: missing={len(expected - actual)}, extra={len(actual - expected)}"
        )


def assert_raw_bindings(
    raw_rows: Sequence[Mapping[str, Any]], prereg: Mapping[str, Any], preregistration_sha256: str | None = None
) -> None:
    for row in raw_rows:
        if row.get("preregistration_id") != prereg.get("preregistration_id"):
            raise RuntimeError("sealed raw preregistration binding mismatch")
        if preregistration_sha256 and row.get("preregistration_sha256") != preregistration_sha256:
            raise RuntimeError("sealed raw preregistration hash binding mismatch")
        reader_id = str(row.get("reader_id") or "")
        reader = (prereg.get("models") or {}).get("readers", {}).get(reader_id)
        if not isinstance(reader, Mapping) or row.get("reader_snapshot_sha256") != _canonical_hash(reader):
            raise RuntimeError("sealed raw reader snapshot binding mismatch")


def _majority(values: Sequence[bool]) -> bool:
    return sum(values) >= 2


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _coverage(scores: Sequence[Mapping[str, Any]]) -> float:
    values = [float(item["entity_coverage_at_5"]) for item in scores if item.get("entity_coverage_at_5") is not None]
    return fmean(values) if values else 0.0


def aggregate_matrix(rows: Sequence[Mapping[str, Any]], *, no_answer_ids: set[str]) -> dict[str, Any]:
    """Aggregate the 2x2 matrix and apply C4-vs-C0 gates per reader."""
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    majority: dict[tuple[str, str, str], bool] = {}
    for row in rows:
        grouped[(str(row["reader_id"]), str(row["arm_id"]))].append(row)
    per_cell: dict[str, dict[str, Any]] = {reader: {} for reader in READERS}
    for reader in READERS:
        for arm in ARMS:
            cell_rows = grouped[(reader, arm)]
            if not cell_rows:
                raise ValueError(f"missing sealed matrix cell: {reader}/{arm}")
            by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for row in cell_rows:
                by_case[str(row["case_id"])].append(row)
            for case_id, case_rows in by_case.items():
                majority[(reader, arm, case_id)] = _majority(
                    [bool(item["score"]["answer_correct"]) for item in case_rows]
                )
            scores = [row["score"] for row in cell_rows]
            no_answer = [row["score"] for row in cell_rows if str(row["case_id"]) in no_answer_ids]
            repeat_accuracy = [
                fmean(float(row["score"]["answer_correct"]) for row in cell_rows if int(row["repeat_index"]) == repeat)
                for repeat in range(REPEATS)
            ]
            recall_latencies = [float(row["recall_latency_seconds"]) for row in cell_rows]
            reader_latencies = [float(row["reader_latency_seconds"]) for row in cell_rows]
            e2e_latencies = [float(row["e2e_latency_seconds"]) for row in cell_rows]
            per_cell[reader][arm] = {
                "accuracy": fmean(float(score["answer_correct"]) for score in scores),
                "majority_accuracy": fmean(float(majority[(reader, arm, case_id)]) for case_id in by_case),
                "accuracy_repeat_stddev": pstdev(repeat_accuracy),
                "entity_coverage_at_5": _coverage(scores),
                "no_answer_accuracy": (
                    fmean(float(score["answer_correct"]) for score in no_answer) if no_answer else 0.0
                ),
                "no_answer_majority_correct": sum(
                    majority[(reader, arm, case_id)] for case_id in by_case if case_id in no_answer_ids
                ),
                "forbidden_violations": sum(bool(score["negative_violation"]) for score in scores),
                "role_modality_confusions": sum(bool(score["role_modality_confusion"]) for score in scores),
                "modality_violations": sum(bool(score["modality_violation"]) for score in scores),
                "provenance_violations": sum(bool(score["provenance_violation"]) for score in scores),
                "leakage_violations": sum(bool(score["leakage_violation"]) for score in scores),
                "packet_budget_violations": sum(bool(row["packet_budget_violation"]) for row in cell_rows),
                "mean_total_tokens": fmean(float(row["usage"]["total_tokens"]) for row in cell_rows),
                "mean_input_tokens": fmean(float(row["usage"]["input_tokens"]) for row in cell_rows),
                "mean_output_tokens": fmean(float(row["usage"]["output_tokens"]) for row in cell_rows),
                "mean_packet_tokens": fmean(float(row["packet_tokens"]) for row in cell_rows),
                "recall_latency_p50_seconds": _percentile(recall_latencies, 0.50),
                "recall_latency_p95_seconds": _percentile(recall_latencies, 0.95),
                "reader_latency_p50_seconds": _percentile(reader_latencies, 0.50),
                "reader_latency_p95_seconds": _percentile(reader_latencies, 0.95),
                "e2e_latency_p95_seconds": _percentile(e2e_latencies, 0.95),
            }

    case_ids = sorted({str(row["case_id"]) for row in rows})
    gates: dict[str, dict[str, Any]] = {}
    paired: dict[str, list[dict[str, Any]]] = {}
    for reader in READERS:
        c0_correct = {case_id for case_id in case_ids if majority[(reader, "C0", case_id)]}
        c4_correct = {case_id for case_id in case_ids if majority[(reader, "C4", case_id)]}
        net_gain = len(c4_correct - c0_correct) - len(c0_correct - c4_correct)
        regressions = len(c0_correct - c4_correct)
        c0 = per_cell[reader]["C0"]
        c4 = per_cell[reader]["C4"]
        gate: dict[str, Any] = {
            "hard_relation_net_gain": net_gain,
            "correct_to_incorrect": regressions,
            "hard_relation_net_gain_ge_2": net_gain >= 2,
            "paired_zero_regression": regressions == 0,
            "entity_coverage_non_decrease": c4["entity_coverage_at_5"] >= c0["entity_coverage_at_5"],
            "hard_entity_coverage_plus_005": c4["entity_coverage_at_5"] >= c0["entity_coverage_at_5"] + 0.05,
            "no_answer_non_decrease": c4["no_answer_majority_correct"] >= c0["no_answer_majority_correct"],
            "forbidden_zero": c0["forbidden_violations"] + c4["forbidden_violations"] == 0,
            "modality_zero": c0["modality_violations"] + c4["modality_violations"] == 0,
            "frozen_evidence_provenance_zero": c0["provenance_violations"] + c4["provenance_violations"] == 0,
            "leakage_zero": c0["leakage_violations"] + c4["leakage_violations"] == 0,
            "packet_budget": c0["packet_budget_violations"] + c4["packet_budget_violations"] == 0,
            "no_extra_recall_llm": True,
            "recall_p50_cost": c4["recall_latency_p50_seconds"] <= c0["recall_latency_p50_seconds"] * 1.15,
            "recall_p95_cost": c4["recall_latency_p95_seconds"]
            <= max(c0["recall_latency_p95_seconds"] + 0.150, c0["recall_latency_p95_seconds"] * 1.25),
        }
        gate["passed"] = all(
            value
            for key, value in gate.items()
            if key not in {"hard_relation_net_gain", "correct_to_incorrect", "passed"}
        )
        gates[reader] = gate
        paired[reader] = [
            {
                "case_id": case_id,
                "c0_majority_correct": majority[(reader, "C0", case_id)],
                "c4_majority_correct": majority[(reader, "C4", case_id)],
            }
            for case_id in case_ids
        ]

    reader_effect: dict[str, Any] = {}
    for arm in ARMS:
        qwen_correct = {case_id for case_id in case_ids if majority[("qwen", arm, case_id)]}
        glm_correct = {case_id for case_id in case_ids if majority[("glm", arm, case_id)]}
        reader_effect[arm] = {
            "glm_only_correct": len(glm_correct - qwen_correct),
            "qwen_only_correct": len(qwen_correct - glm_correct),
            "net_gain": len(glm_correct - qwen_correct) - len(qwen_correct - glm_correct),
            "qwen_correct_to_glm_incorrect": len(qwen_correct - glm_correct),
            "accuracy_delta": per_cell["glm"][arm]["accuracy"] - per_cell["qwen"][arm]["accuracy"],
            "mean_total_tokens_delta": per_cell["glm"][arm]["mean_total_tokens"]
            - per_cell["qwen"][arm]["mean_total_tokens"],
            "reader_p95_seconds_delta": per_cell["glm"][arm]["reader_latency_p95_seconds"]
            - per_cell["qwen"][arm]["reader_latency_p95_seconds"],
        }
    return {"matrix": per_cell, "gates": gates, "paired": paired, "reader_effect": reader_effect}


def _read_latest_complete(path: Path) -> tuple[list[dict[str, Any]], int, int, list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8")
    latest: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    retryable_history = 0
    fatal_history = 0
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                break
            raise
        key = (str(row["case_id"]), int(row["repeat_index"]), str(row["arm_id"]), str(row["reader_id"]))
        history.append(row)
        latest[key] = row
        retryable_history += row.get("status") == "retryable_error"
        fatal_history += row.get("status") == "fatal_error"
    incomplete = [key for key, row in latest.items() if row.get("status") != "complete"]
    if incomplete:
        raise RuntimeError(f"sealed raw has incomplete latest keys: {len(incomplete)}")
    return list(latest.values()), retryable_history, fatal_history, history


def assert_raw_matches_packets(raw_rows: Sequence[Mapping[str, Any]], packet_snapshot: Mapping[str, Any]) -> None:
    """Require every reader cell to use the exact frozen arm packet."""
    frozen_packets = {str(item["packet_key"]): item for item in packet_snapshot.get("packets") or []}
    for row in raw_rows:
        key = f"{row['case_id']}|{row['repeat_index']}|{row['arm_id']}"
        frozen_packet = frozen_packets.get(key)
        if frozen_packet is None:
            raise RuntimeError(f"frozen packet missing: {key}")
        for field in (
            "packet",
            "top5_seed_packet",
            "answerability",
            "recall_latency_seconds",
        ):
            if row.get(field) != frozen_packet.get(field):
                raise RuntimeError(f"sealed raw packet differs from frozen snapshot: {key}.{field}")


def _gold_dict(case: Any) -> dict[str, Any]:
    return dataclasses.asdict(case.gold)


def _public_score(score: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: score.get(key)
        for key in (
            "scorer_version",
            "entity_coverage_at_5",
            "negative_violation",
            "answer_correct",
            "rao_match",
            "packet_rao_match",
            "role_modality_confusion",
            "modality_violation",
            "provenance_violation",
            "leakage_violation",
        )
    }


def _score_rows(
    raw_rows: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, Any],
    prereg: Mapping[str, Any],
    cases: Mapping[str, Any],
) -> list[dict[str, Any]]:
    input_by_id = {str(case["case_id"]): case for case in inputs["cases"]}
    scored: list[dict[str, Any]] = []
    for row in raw_rows:
        case_id = str(row["case_id"])
        case = cases[case_id]
        packet = row["packet"]
        score = frozen.score_visible_case(str(row["predicted_answer"]), packet, _gold_dict(case))
        evidence = frozen.audit_evidence_provenance(packet, input_by_id[case_id], prereg)
        leaks = frozen.audit_leakage(
            {key: value for key, value in row.items() if key not in {"predicted_answer", "status"}}
        )
        score["modality_violation"] = bool(evidence["modality"])
        score["provenance_violation"] = bool(evidence["provenance"])
        score["leakage_violation"] = bool(leaks)
        packet_tokens = sum(int(item["token_count"]) for item in packet)
        scored.append(
            {
                "case_id": case_id,
                "category": case.category,
                "arm_id": row["arm_id"],
                "reader_id": row["reader_id"],
                "repeat_index": row["repeat_index"],
                "score": score,
                "usage": row["usage"],
                "packet_tokens": packet_tokens,
                "recall_latency_seconds": row["recall_latency_seconds"],
                "reader_latency_seconds": row["reader_latency_seconds"],
                "e2e_latency_seconds": row["e2e_latency_seconds"],
                "packet_budget_violation": len(packet) > 10 or packet_tokens > 2000,
            }
        )
    return scored


def _by_category(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["category"]), str(row["reader_id"]), str(row["arm_id"]))].append(row)
    return {
        category: {
            reader: {
                arm: {
                    "accuracy": fmean(
                        float(row["score"]["answer_correct"]) for row in grouped[(category, reader, arm)]
                    ),
                    "entity_coverage_at_5": _coverage([row["score"] for row in grouped[(category, reader, arm)]]),
                }
                for arm in ARMS
            }
            for reader in READERS
        }
        for category in sorted({str(row["category"]) for row in rows})
    }


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# C-series sealed 2x2 validation",
        "",
        "| reader | arm | accuracy | entity@5 | forbidden | modality | leakage | tokens | recall p95 | reader p95 | gate |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for reader in READERS:
        for arm in ARMS:
            cell = report["matrix"][reader][arm]
            gate = "baseline" if arm == "C0" else str(report["gates"][reader]["passed"])
            lines.append(
                f"| {reader} | {arm} | {cell['accuracy']:.4f} | {cell['entity_coverage_at_5']:.4f} | "
                f"{cell['forbidden_violations']} | {cell['modality_violations']} | {cell['leakage_violations']} | "
                f"{cell['mean_total_tokens']:.1f} | {cell['recall_latency_p95_seconds']:.3f} | "
                f"{cell['reader_latency_p95_seconds']:.3f} | {gate} |"
            )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("v1", "v2"), required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--holdout-manifest", type=Path, required=True)
    parser.add_argument("--prereg", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assert_holdout_suite(args.holdout_manifest, args.suite)
    sealed_runner.configure_suite(args.suite)
    prereg = json.loads(args.prereg.read_text(encoding="utf-8"))
    sealed_runner._validate_preregistration(prereg, expected_suite=args.suite)
    assert_implementation_snapshot(prereg)
    if _git("rev-parse", "HEAD") != prereg.get("git_commit"):
        raise RuntimeError("git commit differs from sealed preregistration during scoring")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("sealed scoring requires clean source")
    inputs = json.loads(args.inputs.read_text(encoding="utf-8"))
    packets = json.loads(args.packets.read_text(encoding="utf-8"))
    if prereg.get("packets_sha256") != sha256_file(args.packets):
        raise RuntimeError("sealed packet snapshot hash differs from preregistration")
    if prereg.get("inputs_sha256") != sha256_file(args.inputs):
        raise RuntimeError("sealed input snapshot hash differs from preregistration")
    if frozen.audit_leakage(inputs):
        raise RuntimeError("sealed live inputs contain scorer/gold fields")
    raw_rows, retryable_history, fatal_history, raw_history = _read_latest_complete(args.raw)
    expected = len(inputs["cases"]) * REPEATS * len(ARMS) * len(READERS)
    if len(raw_rows) != expected:
        raise RuntimeError(f"sealed scorer requires {expected} complete unique tasks, got {len(raw_rows)}")
    assert_exact_matrix(raw_rows, inputs)
    assert_raw_bindings(raw_history, prereg, sha256_file(args.prereg))
    assert_raw_matches_packets(raw_rows, packets)
    # Only after every raw reader result is complete and bound may scorer touch
    # snapshot paths that include the sealed payload and design/dev corpora.
    for raw_path, expected_hash in prereg["snapshot_files"].items():
        snapshot_path = Path(raw_path)
        if not snapshot_path.is_file() or sha256_file(snapshot_path) != expected_hash:
            raise RuntimeError(f"sealed scoring snapshot drift: {snapshot_path}")
    dataset = load_sealed_holdout(args.holdout_manifest, allow_sealed=True)
    if prereg.get("sealed_payload_sha256") != load_holdout_manifest(args.holdout_manifest).sha256:
        raise RuntimeError("sealed payload hash differs from preregistration")
    cases = {case.case_id: case for case in dataset.cases}
    if set(cases) != {str(case["case_id"]) for case in inputs["cases"]}:
        raise RuntimeError("sealed scorer case IDs differ from gold-free inputs")
    scored = _score_rows(raw_rows, inputs, prereg, cases)
    no_answer_ids = {case.case_id for case in dataset.cases if case.gold.answerability == "no_answer"}
    aggregates = aggregate_matrix(scored, no_answer_ids=no_answer_ids)
    report = {
        "schema_version": 1,
        "protocol_version": prereg["protocol_version"],
        "scorer_version": "answer-entity-packet-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preregistration_sha256": sha256_file(args.prereg),
        "raw_sha256": sha256_file(args.raw),
        "packets_sha256": sha256_file(args.packets),
        "sealed_payload_sha256": prereg["sealed_payload_sha256"],
        "retryable_history": retryable_history,
        "fatal_history": fatal_history,
        **aggregates,
        "by_category": _by_category(scored),
        "scored_cases": [
            {
                "case_id": row["case_id"],
                "category": row["category"],
                "arm_id": row["arm_id"],
                "reader_id": row["reader_id"],
                "repeat_index": row["repeat_index"],
                "score": _public_score(row["score"]),
                "usage": row["usage"],
                "packet_tokens": row["packet_tokens"],
                "recall_latency_seconds": row["recall_latency_seconds"],
                "reader_latency_seconds": row["reader_latency_seconds"],
                "e2e_latency_seconds": row["e2e_latency_seconds"],
            }
            for row in scored
        ],
    }
    write_json_atomic(args.output, report)
    args.markdown.write_text(_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
