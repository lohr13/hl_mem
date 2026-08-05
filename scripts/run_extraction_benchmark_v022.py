#!/usr/bin/env python
"""Run the v0.22.0 extraction Gold benchmark with the production extractor."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
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
from hl_mem.observability.audit import audit_scope  # noqa: E402
from scripts.eval_against_gold import evaluate_model, load_jsonl  # noqa: E402

DEFAULT_TESTSET = ROOT / "scripts" / "extraction_testset.jsonl"
DEFAULT_GOLD = ROOT / "evaluation" / "datasets" / "extraction_gold_v1.jsonl"
DEFAULT_BASELINE = ROOT / "scripts" / "after_qwen_v0211.jsonl"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "extraction_benchmark_v022.json"
EXPECTED_VERSION = "0.22.0"
EXPECTED_MODEL = "qwen3.7-plus"
EXPECTED_EVENTS = 50
EXPECTED_GOLD_CLAIMS = 73
EXPECTED_SHOULD_MEMORIZE = 14
DEFAULT_VALUE_THRESHOLD = 0.62
DEFAULT_SLEEP_SECONDS = 1.0


class MemoryAuditLogger:
    """Capture audit events in process without writing the production database."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def emit(
        self,
        phase: str,
        action: str,
        outcome: str,
        *,
        detail: Mapping[str, Any] | None = None,
        **dimensions: Any,
    ) -> bool:
        self.entries.append(
            {
                "phase": str(phase),
                "action": str(action),
                "outcome": str(outcome),
                "detail": dict(detail or {}),
                "dimensions": {key: value for key, value in dimensions.items() if value is not None},
            }
        )
        return True

    @contextmanager
    def span(self, phase: str, action: str, **dimensions: Any) -> Iterator[dict[str, Any]]:
        detail: dict[str, Any] = {}
        started = time.perf_counter_ns()
        try:
            yield detail
        except Exception as error:
            detail.update(error_class=type(error).__name__, error=str(error).replace("\n", " ")[:256])
            self.emit(
                phase,
                action,
                "error",
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail=detail,
                **dimensions,
            )
            raise
        else:
            self.emit(
                phase,
                action,
                str(detail.pop("outcome", "success")),
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail=detail,
                **dimensions,
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse reproducible benchmark paths and smoke/full-run controls."""
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


def _event_context(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": event.get("session_id"),
        "actor": event.get("actor_type"),
        "actor_type": event.get("actor_type"),
        "source_kind": event.get("category"),
    }


def _serialize_claim(claim: Any) -> dict[str, Any]:
    if is_dataclass(claim):
        payload = asdict(claim)
    elif isinstance(claim, Mapping):
        payload = dict(claim)
    else:
        payload = dict(vars(claim))
    return payload


def summarize_entailment(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract per-claim verifier evidence from an event's captured audit stream."""
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    possible_under_extraction: list[dict[str, Any]] = []
    labels: Counter[str] = Counter()
    for entry in entries:
        action = str(entry.get("action") or "")
        detail = dict(entry.get("detail") or {})
        if action == "entailment_checked":
            label = str(entry.get("outcome") or "unknown")
            labels[label] += 1
            results.append({"support_label": label, **detail})
        elif action == "entailment_verification_failed":
            failures.append({"outcome": str(entry.get("outcome") or "error"), **detail})
        elif action == "possible_under_extraction":
            possible_under_extraction.append(detail)
    return {
        "triggered": bool(results or failures),
        "checked_claims": len(results),
        "label_counts": dict(sorted(labels.items())),
        "failure_count": len(failures),
        "results": results,
        "failures": failures,
        "possible_under_extraction": possible_under_extraction,
    }


def _reset_extractor_diagnostics(extractor: Any) -> None:
    for attribute, value in (
        ("_schema_retry_count", 0),
        ("_repair_count", 0),
        ("_llm_call_count", 0),
        ("_memorize_decisions", []),
    ):
        if hasattr(extractor, attribute):
            setattr(extractor, attribute, value)


def run_single_extraction(extractor: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """Run one real extraction and retain claims, usage, errors, and audit evidence."""
    logger = MemoryAuditLogger()
    started = time.perf_counter()
    claims_data: list[dict[str, Any]] = []
    error_text: str | None = None
    _reset_extractor_diagnostics(extractor)
    try:
        with audit_scope(logger, event_id=str(event["id"])):
            claims = extractor.extract(event["content"], _event_context(event))
        claims_data = [_serialize_claim(claim) for claim in claims]
    except Exception as error:
        error_text = f"{type(error).__name__}: {str(error).replace(chr(10), ' ')[:500]}"

    entailment = summarize_entailment(logger.entries)
    return {
        "event_id": str(event["id"]),
        "category": str(event.get("category") or ""),
        "actor": str(event.get("actor_type") or ""),
        "should_memorize": bool(claims_data),
        "claims_count": len(claims_data),
        "claims_data": claims_data,
        "input_tokens": int(getattr(extractor, "last_input_tokens", 0)),
        "output_tokens": int(getattr(extractor, "last_output_tokens", 0)),
        "total_tokens": int(getattr(extractor, "last_usage_tokens", 0)),
        "llm_call_count": int(getattr(extractor, "_llm_call_count", 0)),
        "schema_retry_count": int(getattr(extractor, "_schema_retry_count", 0)),
        "repair_count": int(getattr(extractor, "_repair_count", 0)),
        "extraction_error": error_text,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "entailment_audit": entailment,
    }


def run_events(
    events: Sequence[Mapping[str, Any]],
    extractor: Any,
    *,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    sleep_func: Callable[[float], Any] = time.sleep,
    progress: Callable[[str], Any] | None = print,
) -> list[dict[str, Any]]:
    """Run events strictly serially and sleep only between adjacent calls."""
    results: list[dict[str, Any]] = []
    total = len(events)
    for index, event in enumerate(events, 1):
        result = run_single_extraction(extractor, event)
        results.append(result)
        if progress is not None:
            status = "ok" if result["extraction_error"] is None else "error"
            progress(
                f"[{index}/{total}] {event['id']} {status} claims={result['claims_count']} "
                f"audit={result['entailment_audit']['checked_claims']} latency_ms={result['latency_ms']}"
            )
        if index < total and sleep_seconds > 0:
            sleep_func(sleep_seconds)
    return results


def _aggregate_entailment(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    labels: Counter[str] = Counter()
    events_triggered = 0
    claims_checked = 0
    failures = 0
    possible_under_extraction_events = 0
    for result in results:
        audit = dict(result.get("entailment_audit") or {})
        events_triggered += int(bool(audit.get("triggered")))
        claims_checked += int(audit.get("checked_claims") or 0)
        failures += int(audit.get("failure_count") or 0)
        possible_under_extraction_events += int(bool(audit.get("possible_under_extraction")))
        labels.update({str(key): int(value) for key, value in dict(audit.get("label_counts") or {}).items()})
    return {
        "events_triggered": events_triggered,
        "claims_checked": claims_checked,
        "label_counts": dict(sorted(labels.items())),
        "failure_count": failures,
        "possible_under_extraction_events": possible_under_extraction_events,
    }


def build_report(
    gold_records: list[dict[str, Any]],
    results: list[dict[str, Any]],
    baseline_results: list[dict[str, Any]],
    *,
    version: str,
    model: str,
    prompt_hash: str,
    value_threshold: float,
    sleep_seconds: float,
) -> dict[str, Any]:
    """Build the requested summary from one shared Gold denominator."""
    current = evaluate_model(gold_records, results, value_threshold=value_threshold)
    baseline = evaluate_model(gold_records, baseline_results, value_threshold=value_threshold)
    metrics = {
        "claim_precision": current["claim_precision"],
        "claim_recall": current["claim_recall"],
        "scope_accuracy": current["scope_accuracy"],
        "should_memorize_accuracy": current["should_memorize_accuracy"],
        "missed_extractions": current["missed"],
        "over_extractions": current["over_extracted"],
    }
    total_gold_claims = sum(len(record.get("gold_claims") or []) for record in gold_records)
    total_extracted_claims = sum(len(result.get("claims_data") or []) for result in results)
    return {
        "version": version,
        "model": model,
        "prompt_hash": prompt_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(gold_records),
        "total_gold_claims": total_gold_claims,
        "total_extracted_claims": total_extracted_claims,
        "metrics": metrics,
        "comparison_to_v0211": {
            "baseline_claim_precision": baseline["claim_precision"],
            "baseline_claim_recall": baseline["claim_recall"],
            "precision_delta": current["claim_precision"] - baseline["claim_precision"],
            "recall_delta": current["claim_recall"] - baseline["claim_recall"],
        },
        "entailment_audit": _aggregate_entailment(results),
        "run_config": {
            "source": "direct_production_extractor",
            "value_threshold": value_threshold,
            "sleep_seconds": sleep_seconds,
            "verification_mode": "audit",
        },
        "error_count": sum(result.get("extraction_error") is not None for result in results),
        "events": results,
    }


def _validate_full_datasets(
    events: Sequence[Mapping[str, Any]],
    gold_records: Sequence[Mapping[str, Any]],
    baseline_results: Sequence[Mapping[str, Any]],
) -> None:
    if len(events) != EXPECTED_EVENTS:
        raise ValueError(f"expected {EXPECTED_EVENTS} test events, found {len(events)}")
    if len(gold_records) != EXPECTED_EVENTS:
        raise ValueError(f"expected {EXPECTED_EVENTS} Gold events, found {len(gold_records)}")
    event_ids = [str(event["id"]) for event in events]
    gold_ids = [str(record["event_id"]) for record in gold_records]
    baseline_ids = [str(result["event_id"]) for result in baseline_results]
    if len(set(event_ids)) != len(event_ids) or len(set(gold_ids)) != len(gold_ids):
        raise ValueError("testset and Gold event IDs must be unique")
    if set(event_ids) != set(gold_ids):
        raise ValueError("testset and Gold event IDs do not match")
    if set(event_ids) != set(baseline_ids):
        raise ValueError("v0.21.1 baseline and testset event IDs do not match")
    gold_claims = sum(len(record.get("gold_claims") or []) for record in gold_records)
    if gold_claims != EXPECTED_GOLD_CLAIMS:
        raise ValueError(f"expected {EXPECTED_GOLD_CLAIMS} Gold claims, found {gold_claims}")
    should_memorize = sum(bool(record.get("should_memorize")) for record in gold_records)
    if should_memorize != EXPECTED_SHOULD_MEMORIZE:
        raise ValueError(f"expected {EXPECTED_SHOULD_MEMORIZE} should_memorize events, found {should_memorize}")


def _select_by_event_ids(rows: Sequence[dict[str, Any]], event_ids: Sequence[str]) -> list[dict[str, Any]]:
    by_id = {str(row["event_id"]): row for row in rows}
    return [by_id[event_id] for event_id in event_ids]


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _resolve_output(args: argparse.Namespace) -> Path:
    if args.output is not None:
        return args.output
    if args.limit is None:
        return DEFAULT_OUTPUT
    return DEFAULT_OUTPUT.with_name(f"extraction_benchmark_v022_smoke{args.limit}.json")


def _print_summary(report: Mapping[str, Any], output: Path) -> None:
    metrics = report["metrics"]
    comparison = report["comparison_to_v0211"]
    print(
        "summary "
        f"precision={metrics['claim_precision']:.4f} recall={metrics['claim_recall']:.4f} "
        f"scope={metrics['scope_accuracy']:.4f} should_memorize={metrics['should_memorize_accuracy']:.4f} "
        f"precision_delta={comparison['precision_delta']:+.4f} recall_delta={comparison['recall_delta']:+.4f}"
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
    gold_records = load_jsonl(args.gold)
    baseline_results = load_jsonl(args.baseline)
    _validate_full_datasets(events, gold_records, baseline_results)

    settings = load_settings(args.config, args.env_file)
    if __version__ != EXPECTED_VERSION:
        raise RuntimeError(f"package version must be {EXPECTED_VERSION}, found {__version__}")
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
        f"benchmark version=v{__version__} model={settings.llm_model} "
        f"prompt={LLM_EXTRACTOR_VERSION} verification={settings.verification_mode} "
        f"events={len(selected_events)} sleep={args.sleep_seconds}s"
    )
    results = run_events(selected_events, extractor, sleep_seconds=args.sleep_seconds)
    report = build_report(
        selected_gold,
        results,
        selected_baseline,
        version=f"v{__version__}",
        model=settings.llm_model,
        prompt_hash=LLM_EXTRACTOR_VERSION,
        value_threshold=args.value_threshold,
        sleep_seconds=args.sleep_seconds,
    )
    _write_json_atomic(output, report)
    _print_summary(report, output)
    if report["error_count"]:
        print(f"extraction errors={report['error_count']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
