#!/usr/bin/env python
"""Run the v0.23 compact-prompt extraction benchmark on Gold v2."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hl_mem import __version__  # noqa: E402
from hl_mem.components import make_extractor  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION  # noqa: E402
from scripts.eval_against_gold import evaluate_model, load_jsonl  # noqa: E402
from scripts.run_extraction_benchmark_v022 import (  # noqa: E402
    _aggregate_entailment,
    _select_by_event_ids,
    _write_json_atomic,
    run_events,
)

DEFAULT_TESTSET = ROOT / "scripts" / "extraction_testset.jsonl"
DEFAULT_GOLD = ROOT / "evaluation" / "datasets" / "extraction_gold_v2.jsonl"
DEFAULT_BASELINE = ROOT / "evaluation" / "results" / "extraction_benchmark_v022.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "extraction_benchmark_v023.json"
BENCHMARK_VERSION = "v0.23"
BASELINE_LABEL = "v0.22.0"
EXPECTED_MODEL = "qwen3.7-plus"
EXPECTED_EVENTS = 50
EXPECTED_GOLD_CLAIMS = 149
EXPECTED_SHOULD_MEMORIZE = 24
DEFAULT_VALUE_THRESHOLD = 0.62
DEFAULT_SLEEP_SECONDS = 1.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse benchmark paths and smoke/full-run controls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "hl_mem.toml")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--value-threshold", type=float, default=DEFAULT_VALUE_THRESHOLD)
    return parser.parse_args(argv)


def load_positive_gold(path: Path) -> list[dict[str, Any]]:
    """Load Gold v2 and keep only explicitly positive claims."""
    records: list[dict[str, Any]] = []
    for original in load_jsonl(path):
        record = dict(original)
        record["gold_claims"] = [
            dict(claim) for claim in original.get("gold_claims") or [] if claim.get("label") == "gold_positive"
        ]
        record["should_memorize"] = bool(record["gold_claims"])
        records.append(record)
    return records


def load_baseline_report(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the v0.22 JSON report and its per-event predictions."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"baseline must be a JSON object: {path}")
    events = payload.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError(f"baseline report does not contain an events array: {path}")
    return payload, [dict(event) for event in events]


def validate_full_datasets(
    events: Sequence[Mapping[str, Any]],
    gold_records: Sequence[Mapping[str, Any]],
    baseline_results: Sequence[Mapping[str, Any]],
) -> None:
    """Validate the frozen 50-event / 149-positive benchmark inputs."""
    if len(events) != EXPECTED_EVENTS:
        raise ValueError(f"expected {EXPECTED_EVENTS} test events, found {len(events)}")
    if len(gold_records) != EXPECTED_EVENTS:
        raise ValueError(f"expected {EXPECTED_EVENTS} Gold events, found {len(gold_records)}")
    if len(baseline_results) != EXPECTED_EVENTS:
        raise ValueError(f"expected {EXPECTED_EVENTS} baseline events, found {len(baseline_results)}")

    event_ids = [str(event["id"]) for event in events]
    gold_ids = [str(record["event_id"]) for record in gold_records]
    baseline_ids = [str(result["event_id"]) for result in baseline_results]
    if len(set(event_ids)) != len(event_ids) or len(set(gold_ids)) != len(gold_ids):
        raise ValueError("testset and Gold event IDs must be unique")
    if set(event_ids) != set(gold_ids) or set(event_ids) != set(baseline_ids):
        raise ValueError("testset, Gold v2, and v0.22 baseline event IDs must match")

    gold_claims = sum(len(record.get("gold_claims") or []) for record in gold_records)
    if gold_claims != EXPECTED_GOLD_CLAIMS:
        raise ValueError(f"expected {EXPECTED_GOLD_CLAIMS} positive Gold claims, found {gold_claims}")
    should_memorize = sum(bool(record.get("should_memorize")) for record in gold_records)
    if should_memorize != EXPECTED_SHOULD_MEMORIZE:
        raise ValueError(f"expected {EXPECTED_SHOULD_MEMORIZE} should_memorize events, found {should_memorize}")


def _metric_payload(stats: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "claim_precision": stats["claim_precision"],
        "claim_recall": stats["claim_recall"],
        "scope_accuracy": stats["scope_accuracy"],
        "should_memorize_accuracy": stats["should_memorize_accuracy"],
        "missed_extractions": stats["missed"],
        "over_extractions": stats["over_extracted"],
    }


def build_report(
    gold_records: list[dict[str, Any]],
    results: list[dict[str, Any]],
    baseline_results: list[dict[str, Any]],
    baseline_report: Mapping[str, Any],
    *,
    model: str,
    prompt_hash: str,
    value_threshold: float,
    sleep_seconds: float,
) -> dict[str, Any]:
    """Evaluate current and v0.22 predictions on the same filtered Gold v2."""
    current = evaluate_model(gold_records, results, value_threshold=value_threshold)
    baseline = evaluate_model(gold_records, baseline_results, value_threshold=value_threshold)
    total_extracted = sum(len(result.get("claims_data") or []) for result in results)
    baseline_extracted = sum(len(result.get("claims_data") or []) for result in baseline_results)
    return {
        "version": BENCHMARK_VERSION,
        "package_version": f"v{__version__}",
        "model": model,
        "prompt_hash": prompt_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(gold_records),
        "total_gold_claims": sum(len(record.get("gold_claims") or []) for record in gold_records),
        "total_extracted_claims": total_extracted,
        "metrics": _metric_payload(current),
        "comparison_to_v022": {
            "baseline_version": str(baseline_report.get("version") or BASELINE_LABEL),
            "baseline_prompt_hash": baseline_report.get("prompt_hash"),
            "baseline_total_extracted_claims": baseline_extracted,
            "baseline_metrics_on_gold_v2": _metric_payload(baseline),
            "precision_delta": current["claim_precision"] - baseline["claim_precision"],
            "recall_delta": current["claim_recall"] - baseline["claim_recall"],
            "extracted_delta": total_extracted - baseline_extracted,
            "missed_delta": current["missed"] - baseline["missed"],
            "over_extracted_delta": current["over_extracted"] - baseline["over_extracted"],
        },
        "entailment_audit": _aggregate_entailment(results),
        "run_config": {
            "source": "direct_production_extractor",
            "gold": "extraction_gold_v2:gold_positive",
            "value_threshold": value_threshold,
            "sleep_seconds": sleep_seconds,
            "verification_mode": "audit",
        },
        "error_count": sum(result.get("extraction_error") is not None for result in results),
        "events": results,
    }


def _resolve_output(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output
    if args.limit is None:
        return DEFAULT_OUTPUT
    return DEFAULT_OUTPUT.with_name(f"extraction_benchmark_v023_smoke{args.limit}.json")


def print_comparison(report: Mapping[str, Any], output: Path) -> None:
    """Print the requested old-vs-new Markdown comparison table."""
    current = report["metrics"]
    comparison = report["comparison_to_v022"]
    baseline = comparison["baseline_metrics_on_gold_v2"]
    print(f"=== {BASELINE_LABEL} (old prompt) vs v0.23 (new prompt) on Gold v2 ===")
    print("| Metric | v0.22.0 | v0.23 | Delta |")
    print("|---|---:|---:|---:|")
    print(
        f"| Precision | {baseline['claim_precision']:.1%} | {current['claim_precision']:.1%} | "
        f"{comparison['precision_delta']:+.1%} |"
    )
    print(
        f"| Recall | {baseline['claim_recall']:.1%} | {current['claim_recall']:.1%} | "
        f"{comparison['recall_delta']:+.1%} |"
    )
    print(
        f"| Extracted | {comparison['baseline_total_extracted_claims']} | "
        f"{report['total_extracted_claims']} | {comparison['extracted_delta']:+d} |"
    )
    print(
        f"| Missed | {baseline['missed_extractions']} | {current['missed_extractions']} | "
        f"{comparison['missed_delta']:+d} |"
    )
    print(
        f"| Over-extracted | {baseline['over_extractions']} | {current['over_extractions']} | "
        f"{comparison['over_extracted_delta']:+d} |"
    )
    print(f"result={output}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit is not None and not 1 <= args.limit <= EXPECTED_EVENTS:
        raise ValueError(f"--limit must be between 1 and {EXPECTED_EVENTS}")
    if args.sleep_seconds < 0:
        raise ValueError("--sleep-seconds must be non-negative")
    if not 0.0 <= args.value_threshold <= 1.0:
        raise ValueError("--value-threshold must be between 0 and 1")

    events = load_jsonl(args.testset)
    gold_records = load_positive_gold(args.gold)
    baseline_report, baseline_results = load_baseline_report(args.baseline)
    validate_full_datasets(events, gold_records, baseline_results)

    settings = load_settings(args.config, args.env_file)
    if settings.llm_model != EXPECTED_MODEL:
        raise RuntimeError(f"llm.model must be {EXPECTED_MODEL}, found {settings.llm_model}")
    if settings.verification_mode != "audit":
        raise RuntimeError(f"extraction.verification_mode must be audit, found {settings.verification_mode}")
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required")

    selected_events = events[: args.limit] if args.limit is not None else events
    selected_ids = [str(event["id"]) for event in selected_events]
    selected_gold = _select_by_event_ids(gold_records, selected_ids)
    selected_baseline = _select_by_event_ids(baseline_results, selected_ids)
    output = _resolve_output(args)
    extractor = make_extractor(settings, require_real=True)

    print(
        f"benchmark version={BENCHMARK_VERSION} package=v{__version__} model={settings.llm_model} "
        f"prompt={LLM_EXTRACTOR_VERSION} verification={settings.verification_mode} "
        f"events={len(selected_events)} sleep={args.sleep_seconds}s"
    )
    results = run_events(selected_events, extractor, sleep_seconds=args.sleep_seconds)
    report = build_report(
        selected_gold,
        results,
        selected_baseline,
        baseline_report,
        model=settings.llm_model,
        prompt_hash=LLM_EXTRACTOR_VERSION,
        value_threshold=args.value_threshold,
        sleep_seconds=args.sleep_seconds,
    )
    _write_json_atomic(output, report)
    print_comparison(report, output)
    if report["error_count"]:
        print(f"extraction errors={report['error_count']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
