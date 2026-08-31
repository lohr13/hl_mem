"""Run the required zero-network public recall release gate."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from hl_mem.settings import Settings
from tests.eval.eval_runner import _load_rows, _sha256_utf8_lf, run
from tests.eval.fixtures.build_ci_snapshot import build_ci_snapshot
from tests.eval.gate_check import check

EVAL_ROOT = Path(__file__).parent
PUBLIC_ROOT = EVAL_ROOT / "public"
DEFAULT_DATASET = PUBLIC_ROOT / "recall_core_v1.jsonl"
DEFAULT_MANIFEST = PUBLIC_ROOT / "recall_core_v1.manifest.json"
DEFAULT_PROTOCOL = PUBLIC_ROOT / "recall_core_v1.protocol.json"
DEFAULT_BASELINE = PUBLIC_ROOT / "recall_core_v1.baseline.json"
BASELINE_STATUS = "public_release_baseline"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _validate_contracts(
    dataset: Path,
    manifest: dict[str, Any],
    protocol_path: Path,
    protocol: dict[str, Any],
) -> list[str]:
    rows = _load_rows(dataset)
    slice_counts = dict(sorted(Counter(str(row["slice"]) for row in rows).items()))
    failures: list[str] = []
    if manifest.get("dataset_sha256_algorithm") != "sha256-utf8-lf-v1":
        failures.append("manifest dataset hash algorithm must be sha256-utf8-lf-v1")
    if manifest.get("dataset_sha256") != _sha256_utf8_lf(dataset):
        failures.append("dataset hash does not match manifest")
    if manifest.get("protocol_sha256") != _sha256_utf8_lf(protocol_path):
        failures.append("protocol hash does not match manifest")
    if manifest.get("case_count") != len(rows):
        failures.append("dataset case count does not match manifest")
    if manifest.get("slice_counts") != slice_counts:
        failures.append("dataset slice counts do not match manifest")
    expected = {
        "protocol_version": "core-recall-public-v1",
        "top_k": 5,
        "embedding": {"provider": "fake", "model": "fake", "dim": 2048},
        "extractor": {"provider": "fake", "model": "fake-v1"},
        "reranker": {"mode": "off"},
        "index_text_mode": "legacy",
        "max_metric_regression": 0.01,
        "max_slice_regression": 0.05,
        "required_http_success_rate": 1.0,
        "required_forbidden_hits": 0,
    }
    if protocol != expected:
        failures.append("public recall protocol does not match the frozen v1 contract")
    return failures


def _settings(protocol: dict[str, Any]) -> Settings:
    embedding = protocol["embedding"]
    return replace(
        Settings.for_test(),
        embedder_mode=str(embedding["provider"]),
        embedding_dim=int(embedding["dim"]),
        extractor_mode=str(protocol["extractor"]["provider"]),
        reranker_mode=str(protocol["reranker"]["mode"]),
        index_text_mode=str(protocol["index_text_mode"]),
    )


def _baseline(report: dict[str, Any], protocol: dict[str, Any], protocol_path: Path) -> dict[str, Any]:
    fixture = report["artifacts"]["fixture"]
    return {
        "status": BASELINE_STATUS,
        "schema_version": report["schema_version"],
        "protocol_version": protocol["protocol_version"],
        "protocol_sha256": _sha256_utf8_lf(protocol_path),
        "dataset_sha256": report["artifacts"]["dataset_sha256"],
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": fixture["fixture_sha256"],
        "case_count": report["case_count"],
        "slice_counts": report["slice_counts"],
        "metrics": report["metrics"],
        "slices": report["slices"],
        "http_success_rate": report["http_success_rate"],
        "total_forbidden_hits": report["total_forbidden_hits"],
        "warning": "Synthetic zero-network regression evidence; not a real-provider quality score.",
    }


def _run_report(dataset: Path, protocol: dict[str, Any], protocol_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="hl-mem-public-recall-") as temporary_directory:
        snapshot = Path(temporary_directory) / "recall-core-v1.db"
        build_ci_snapshot(snapshot, dataset)
        report = run(snapshot, dataset, int(protocol["top_k"]), settings=_settings(protocol))
    report["artifacts"]["protocol_sha256"] = _sha256_utf8_lf(protocol_path)
    report["protocol_version"] = protocol["protocol_version"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--write-baseline", type=Path)
    arguments = parser.parse_args(argv)

    try:
        manifest = _load(arguments.manifest)
        protocol = _load(arguments.protocol)
        failures = _validate_contracts(arguments.dataset, manifest, arguments.protocol, protocol)
        if failures:
            raise ValueError("; ".join(failures))
        report = _run_report(arguments.dataset, protocol, arguments.protocol)
        if arguments.report:
            _write(arguments.report, report)
        if arguments.write_baseline:
            if arguments.write_baseline.exists():
                raise FileExistsError(f"baseline already exists: {arguments.write_baseline}")
            _write(arguments.write_baseline, _baseline(report, protocol, arguments.protocol))
            print(f"Public recall baseline written: {arguments.write_baseline}")
            return 0
        baseline = _load(arguments.baseline)
        if baseline.get("status") != BASELINE_STATUS:
            raise ValueError(f"baseline status must be {BASELINE_STATUS}")
        if baseline.get("protocol_sha256") != _sha256_utf8_lf(arguments.protocol):
            raise ValueError("baseline protocol hash does not match frozen protocol")
        failures = check(
            report,
            baseline,
            tolerance=float(protocol["max_metric_regression"]),
            slice_tolerance=float(protocol["max_slice_regression"]),
        )
    except (FileNotFoundError, FileExistsError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Recall public fixture gate: FAILED\n- {error}")
        return 1

    if failures:
        print("Recall public fixture gate: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    metrics = report["metrics"]
    print(
        "Recall public fixture gate: PASSED | "
        f"Recall@5={metrics['recall_at_5']:.4f} MRR={metrics['mrr']:.4f} "
        f"no-answer P/R={metrics['no_answer_precision']:.4f}/{metrics['no_answer_recall']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
