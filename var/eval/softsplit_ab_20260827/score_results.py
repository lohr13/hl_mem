"""Score compact==20 A/B JSONL against the three frozen protocol gates."""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROTOCOL_ID = "softsplit_ab_20260827_v1"
EQUIPMENT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = EQUIPMENT_DIR / "manifest.json"
DEFAULT_RUNS = EQUIPMENT_DIR / "runs.jsonl"
DEFAULT_OUTPUT = EQUIPMENT_DIR / "score.json"


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _duplicate_totals(records: list[Mapping[str, Any]], arm: str) -> tuple[int, int, bool]:
    claims = 0
    duplicates = 0
    complete = True
    for record in records:
        profile = record.get(arm, {}).get("duplicate_profile") if isinstance(record.get(arm), Mapping) else None
        if not isinstance(profile, Mapping) or profile.get("error"):
            complete = False
            continue
        try:
            claims += int(profile["claim_count"])
            duplicates += int(profile["duplicate_count"])
        except (KeyError, TypeError, ValueError):
            complete = False
    return claims, duplicates, complete


def score_records(records: list[Mapping[str, Any]], *, expected_case_count: int) -> dict[str, Any]:
    if expected_case_count < 1:
        raise ValueError("expected_case_count must be positive")
    by_case: dict[str, Mapping[str, Any]] = {}
    duplicate_case_ids: list[str] = []
    for record in records:
        case_id = str(record.get("case_id") or "")
        if not case_id:
            raise ValueError("every run record must have a case_id")
        if case_id in by_case:
            duplicate_case_ids.append(case_id)
        by_case[case_id] = record
    observed = list(by_case.values())
    missing_case_count = max(0, expected_case_count - len(observed))
    complete_corpus = len(observed) == expected_case_count and not duplicate_case_ids

    net_new_values: list[int] = []
    for record in observed:
        try:
            net_new_values.append(max(0, int(record.get("comparison", {})["net_new_after_split"])))
        except (KeyError, TypeError, ValueError):
            net_new_values.append(0)
    net_new_values.extend([0] * missing_case_count)
    median_net_new = float(statistics.median(net_new_values))
    cases_at_least_two = sum(value >= 2 for value in net_new_values)
    fraction_at_least_two = _rate(cases_at_least_two, expected_case_count)
    effective_pass = complete_corpus and median_net_new >= 3 and fraction_at_least_two >= 0.5

    control_claims, control_duplicates, control_complete = _duplicate_totals(observed, "control")
    treatment_claims, treatment_duplicates, treatment_complete = _duplicate_totals(observed, "treatment")
    control_duplicate_rate = _rate(control_duplicates, control_claims)
    treatment_duplicate_rate = _rate(treatment_duplicates, treatment_claims)
    duplicate_delta_pp = (treatment_duplicate_rate - control_duplicate_rate) * 100
    duplicate_pass = (
        complete_corpus
        and control_complete
        and treatment_complete
        and control_claims > 0
        and treatment_claims > 0
        and duplicate_delta_pp <= 5.0
    )

    expected_treatment_requests = expected_case_count * 3
    failed_or_missing_requests = missing_case_count * 3
    request_metrics_complete = True
    for record in observed:
        treatment = record.get("treatment")
        summary = treatment.get("request_summary") if isinstance(treatment, Mapping) else None
        if not isinstance(summary, Mapping):
            failed_or_missing_requests += 3
            request_metrics_complete = False
            continue
        try:
            failed_or_missing_requests += int(summary["failed_or_missing_count"])
        except (KeyError, TypeError, ValueError):
            failed_or_missing_requests += 3
            request_metrics_complete = False
    failure_rate = _rate(failed_or_missing_requests, expected_treatment_requests)
    request_pass = complete_corpus and request_metrics_complete and failure_rate <= 0.02

    gates = {
        "effective_output": {
            "status": "PASS" if effective_pass else "FAIL",
            "median_net_new_after_split": median_net_new,
            "cases_net_new_at_least_2": cases_at_least_two,
            "fraction_net_new_at_least_2": fraction_at_least_two,
            "thresholds": {"median_min": 3, "fraction_min": 0.5},
        },
        "duplicate_pollution": {
            "status": "PASS" if duplicate_pass else "FAIL",
            "control_duplicate_rate": control_duplicate_rate,
            "treatment_duplicate_rate": treatment_duplicate_rate,
            "delta_pp": round(duplicate_delta_pp, 6),
            "threshold_delta_pp_max": 5.0,
            "control": {"claim_count": control_claims, "duplicate_count": control_duplicates},
            "treatment": {"claim_count": treatment_claims, "duplicate_count": treatment_duplicates},
            "metrics_complete": control_complete and treatment_complete,
        },
        "request_failure": {
            "status": "PASS" if request_pass else "FAIL",
            "failed_or_missing_requests": failed_or_missing_requests,
            "expected_treatment_requests": expected_treatment_requests,
            "failure_rate": failure_rate,
            "threshold_max": 0.02,
            "metrics_complete": request_metrics_complete,
        },
    }
    overall_pass = all(gate["status"] == "PASS" for gate in gates.values())
    return {
        "protocol_id": PROTOCOL_ID,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "overall": "PASS" if overall_pass else "FAIL",
        "corpus": {
            "expected_case_count": expected_case_count,
            "observed_case_count": len(observed),
            "missing_case_count": missing_case_count,
            "duplicate_case_ids": sorted(set(duplicate_case_ids)),
            "complete": complete_corpus,
        },
        "gates": gates,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid JSONL at line {line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {line_number} is not an object")
        records.append(value)
    return records


def score_files(manifest_path: Path, runs_path: Path, output_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol_id") != PROTOCOL_ID:
        raise ValueError("manifest protocol_id does not match")
    report = score_records(_load_jsonl(runs_path), expected_case_count=int(manifest["case_count"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = score_files(args.manifest, args.runs, args.output)
    print(json.dumps({"output": str(args.output), "overall": report["overall"]}, ensure_ascii=False))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
