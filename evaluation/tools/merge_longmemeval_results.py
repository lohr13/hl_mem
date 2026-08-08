#!/usr/bin/env python
"""Merge LongMemEval shard reports into one validated schema v1 report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools.run_longmemeval_benchmark import (  # noqa: E402
    DEFAULT_DATASET,
    _dataset_complete,
    _file_sha256,
    _write_json_atomic,
    aggregate_results,
    iter_case_records,
    normalize_case,
)

DEFAULT_PATTERN = "longmemeval_shard_*.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "longmemeval_s_benchmark.json"
CONFIGURATION_FIELDS = (
    "extractor",
    "extractor_provider",
    "extractor_version",
    "embedder",
    "embedding_dim",
    "embedding_api_mode",
    "embedding_text_type",
    "reranker",
    "reader",
    "judge",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--pattern", default=DEFAULT_PATTERN)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--require-cases", type=int)
    parser.add_argument("--require-no-errors", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def _read_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"shard report must contain a JSON object: {path}")
    if payload.get("schema_version") != 1:
        raise ValueError(f"shard report schema_version must be 1: {path}")
    if payload.get("benchmark") != "LongMemEval-S":
        raise ValueError(f"shard report benchmark must be LongMemEval-S: {path}")
    return payload


def _configuration_identity(report: Mapping[str, Any], path: Path) -> dict[str, Any]:
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ValueError(f"shard report is missing run metadata: {path}")
    models = run.get("models")
    if not isinstance(models, Mapping):
        raise ValueError(f"shard report is missing run.models: {path}")
    missing = [field for field in CONFIGURATION_FIELDS if field not in models]
    if missing:
        raise ValueError(f"shard report is missing extractor/embedder configuration {missing}: {path}")
    qa_enabled = run.get("qa_enabled")
    if not isinstance(qa_enabled, bool):
        raise ValueError(f"shard report run.qa_enabled must be boolean: {path}")
    package_version = run.get("package_version")
    if not isinstance(package_version, str) or not package_version:
        raise ValueError(f"shard report is missing run.package_version: {path}")
    reader_context_mode = str(run.get("reader_context_mode") or "head")
    if reader_context_mode not in {"head", "windowed"}:
        raise ValueError(f"shard report has unsupported run.reader_context_mode: {path}")
    return {
        **{field: models[field] for field in CONFIGURATION_FIELDS},
        "qa_enabled": qa_enabled,
        "reader_context_mode": reader_context_mode,
        "package_version": package_version,
    }


def _dataset_hash(report: Mapping[str, Any], path: Path) -> str:
    dataset = report.get("dataset")
    sha256 = dataset.get("sha256") if isinstance(dataset, Mapping) else None
    if not isinstance(sha256, str) or not sha256:
        raise ValueError(f"shard report is missing dataset.sha256: {path}")
    return sha256


def _validated_case(raw_case: object, path: Path, *, qa_enabled: bool) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        raise ValueError(f"shard report contains a non-object case: {path}")
    case_id = str(raw_case.get("case_id") or "").strip()
    if not case_id:
        raise ValueError(f"shard report contains a case without case_id: {path}")
    question_type = raw_case.get("question_type")
    if not isinstance(question_type, str) or not question_type.strip():
        raise ValueError(f"case {case_id!r} is missing a string question_type: {path}")
    if "error" not in raw_case:
        raise ValueError(f"case {case_id!r} is missing required field 'error': {path}")
    error = raw_case["error"]
    if error is not None and not isinstance(error, str):
        raise ValueError(f"case {case_id!r} error must be null or string: {path}")
    retrieval = raw_case.get("retrieval")
    if retrieval is not None:
        if not isinstance(retrieval, Mapping) or not isinstance(retrieval.get("eligible"), bool):
            raise ValueError(f"case {case_id!r} retrieval must contain boolean eligible: {path}")
    elif error is None:
        raise ValueError(f"successful case {case_id!r} is missing retrieval metrics: {path}")
    qa = raw_case.get("qa")
    if qa is not None and (not isinstance(qa, Mapping) or not isinstance(qa.get("correct"), bool)):
        raise ValueError(f"case {case_id!r} qa must contain boolean correct: {path}")
    if error is None and qa_enabled and qa is None:
        raise ValueError(f"successful QA-enabled case {case_id!r} is missing qa metrics: {path}")
    if error is None and not qa_enabled and qa is not None:
        raise ValueError(f"QA-disabled case {case_id!r} unexpectedly contains qa metrics: {path}")
    return dict(raw_case)


def _dataset_order(dataset: Path) -> dict[str, int]:
    order: dict[str, int] = {}
    for index, record in enumerate(iter_case_records(dataset)):
        case_id = normalize_case(record).case_id
        if case_id in order:
            raise ValueError(f"dataset contains duplicate case_id {case_id!r}")
        order[case_id] = index
    return order


def merge_reports(
    *,
    input_dir: Path,
    pattern: str,
    dataset: Path,
    require_cases: int | None = None,
    require_no_errors: bool = False,
    exclude: Path | None = None,
) -> dict[str, Any]:
    if require_cases is not None and require_cases < 1:
        raise ValueError("--require-cases must be a positive integer")
    if not input_dir.is_dir():
        raise FileNotFoundError(f"input directory does not exist: {input_dir}")
    excluded = exclude.resolve() if exclude is not None else None
    paths = sorted(
        path for path in input_dir.glob(pattern) if path.is_file() and (excluded is None or path.resolve() != excluded)
    )
    if not paths:
        raise FileNotFoundError(f"no shard reports match {pattern!r} in {input_dir}")

    dataset_sha256 = _file_sha256(dataset)
    reports: list[dict[str, Any]] = []
    cases_by_id: dict[str, dict[str, Any]] = {}
    source_by_case_id: dict[str, Path] = {}
    expected_configuration: dict[str, Any] | None = None
    for path in paths:
        report = _read_report(path)
        if _dataset_hash(report, path) != dataset_sha256:
            raise ValueError(f"dataset sha256 mismatch in shard report: {path}")
        configuration = _configuration_identity(report, path)
        if expected_configuration is None:
            expected_configuration = configuration
        elif configuration != expected_configuration:
            raise ValueError(f"run/model configuration mismatch in shard report: {path}")
        raw_cases = report.get("cases")
        if not isinstance(raw_cases, list):
            raise ValueError(f"shard report cases must be a JSON array: {path}")
        for raw_case in raw_cases:
            case = _validated_case(raw_case, path, qa_enabled=bool(configuration["qa_enabled"]))
            case_id = str(case["case_id"])
            if case_id in cases_by_id:
                raise ValueError(f"duplicate case_id {case_id!r} in {source_by_case_id[case_id]} and {path}")
            cases_by_id[case_id] = case
            source_by_case_id[case_id] = path
        reports.append(report)

    if require_cases is not None and len(cases_by_id) != require_cases:
        raise ValueError(f"expected exactly {require_cases} cases, found {len(cases_by_id)}")
    error_case_ids = [case_id for case_id, case in cases_by_id.items() if case.get("error")]
    if require_no_errors and error_case_ids:
        raise ValueError(f"error cases are not allowed: {sorted(error_case_ids)}")

    order = _dataset_order(dataset)
    unknown_case_ids = set(cases_by_id) - set(order)
    if unknown_case_ids:
        raise ValueError(f"shard reports contain case_ids absent from dataset: {sorted(unknown_case_ids)}")
    cases = sorted(cases_by_id.values(), key=lambda case: order[str(case["case_id"])])

    first_run = reports[0].get("run")
    run: dict[str, Any] = dict(first_run) if isinstance(first_run, Mapping) else {}
    started_values = [
        shard_run["started_at"]
        for report in reports
        if isinstance((shard_run := report.get("run")), Mapping) and isinstance(shard_run.get("started_at"), str)
    ]
    if started_values:
        run["started_at"] = min(started_values)
    if expected_configuration is not None:
        run["reader_context_mode"] = expected_configuration["reader_context_mode"]
    run.update(
        {
            "limit": len(cases),
            "offset": 0,
            "resume": False,
            "config_compare": False,
            "merge": {
                "input_dir": str(input_dir.resolve()),
                "pattern": pattern,
                "source_files": [str(path.resolve()) for path in paths],
                "require_cases": require_cases,
                "require_no_errors": require_no_errors,
            },
        }
    )
    return {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "status": "completed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(dataset.resolve()),
            "bytes": dataset.stat().st_size,
            "complete_json_array": _dataset_complete(dataset),
            "sha256": dataset_sha256,
        },
        "run": run,
        "metrics": aggregate_results(cases),
        "cases": cases,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = merge_reports(
        input_dir=args.input_dir,
        pattern=args.pattern,
        dataset=args.dataset,
        require_cases=args.require_cases,
        require_no_errors=args.require_no_errors,
        exclude=args.output,
    )
    _write_json_atomic(args.output, report)
    overall = report["metrics"]["overall"]
    print(
        f"merged cases={overall['cases']} failures={overall['failed_cases']} output={args.output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
