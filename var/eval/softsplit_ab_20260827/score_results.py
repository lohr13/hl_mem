"""Score compact==20 A/B JSONL against the frozen v2 protocol gates."""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MANIFEST_PROTOCOL_ID = "softsplit_ab_20260827_v1"
PROTOCOL_ID = "softsplit_ab_20260827_v2"
DELTA_REPAIR_PROTOCOL_ID = "softsplit_ab_20260827_v3"
EQUIPMENT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = EQUIPMENT_DIR / "manifest.json"
DEFAULT_RUNS = EQUIPMENT_DIR / "runs.jsonl"
DEFAULT_OUTPUT = EQUIPMENT_DIR / "score.json"
CASE_CATEGORIES = (
    "success",
    "runner_error",
    "replay_drift",
    "api_error",
    "protocol_deviation",
)
TRANSPORT_ERROR_CLASSES = frozenset(
    {
        "HTTPStatusError",
        "TransportError",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutError",
        "NetworkError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "CloseError",
        "ConnectionError",
        "ProtocolError",
        "LocalProtocolError",
        "RemoteProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
    }
)
EXTRACTION_QUALITY_ERROR_CLASSES = frozenset(
    {
        "LLMSchemaValidationError",
        "LLMOutputTruncatedError",
    }
)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _arm(record: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = record.get(name)
    return value if isinstance(value, Mapping) else {}


def _requests(record: Mapping[str, Any], arm: str) -> list[Mapping[str, Any]]:
    value = _arm(record, arm).get("requests")
    if not isinstance(value, list):
        return []
    return [request for request in value if isinstance(request, Mapping)]


def _non_cache_requests(record: Mapping[str, Any], arm: str) -> list[Mapping[str, Any]]:
    return [request for request in _requests(record, arm) if not bool(request.get("cache_hit"))]


def _error_class(error: Any) -> str | None:
    if not isinstance(error, Mapping):
        return None
    value = error.get("class")
    if not isinstance(value, str) or not value:
        return None
    return value.rsplit(".", 1)[-1]


def _transport_failure_count(record: Mapping[str, Any], arm: str) -> int:
    return sum(
        request.get("status") == "error" and _error_class(request.get("error")) in TRANSPORT_ERROR_CLASSES
        for request in _non_cache_requests(record, arm)
    )


def _extraction_failure_count(record: Mapping[str, Any], arm: str) -> int:
    return int(_error_class(_arm_error(record, arm)) in EXTRACTION_QUALITY_ERROR_CLASSES)


def _failed_request_count(record: Mapping[str, Any], arm: str) -> int:
    return sum(request.get("status") == "error" for request in _requests(record, arm))


def _arm_error(record: Mapping[str, Any], arm: str) -> Mapping[str, Any] | None:
    value = _arm(record, arm).get("error")
    return value if isinstance(value, Mapping) else None


def classify_record(record: Mapping[str, Any]) -> str:
    """Assign one mutually exclusive attribution category to a run case."""
    reasons_value = record.get("failure_reasons")
    reasons = set(reasons_value) if isinstance(reasons_value, list) else set()
    if "runner_error" in reasons:
        return "runner_error"
    has_api_or_extraction_error = any(
        _arm_error(record, arm) is not None or _failed_request_count(record, arm) > 0
        for arm in ("control", "treatment")
    )
    if has_api_or_extraction_error:
        return "api_error"
    if "control_root_not_compact_exact_20" in reasons:
        return "replay_drift"
    if record.get("status") != "success" or reasons:
        return "protocol_deviation"
    return "success"


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


def _duplicate_rate_for_record(record: Mapping[str, Any], arm: str) -> float | None:
    profile = _arm(record, arm).get("duplicate_profile")
    if not isinstance(profile, Mapping) or profile.get("error"):
        return None
    try:
        return _rate(int(profile["duplicate_count"]), int(profile["claim_count"]))
    except (KeyError, TypeError, ValueError):
        return None


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
    categories = {str(record["case_id"]): classify_record(record) for record in observed}
    category_records = {
        category: [record for record in observed if categories[str(record["case_id"])] == category]
        for category in CASE_CATEGORIES
    }
    case_counts = {category: len(category_records[category]) for category in CASE_CATEGORIES}

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
    successful_net_new = [
        max(0, int(record.get("comparison", {}).get("net_new_after_split", 0)))
        for record in category_records["success"]
    ]

    duplicate_records = [record for record in observed if categories[str(record["case_id"])] != "runner_error"]
    control_claims, control_duplicates, control_complete = _duplicate_totals(duplicate_records, "control")
    treatment_claims, treatment_duplicates, treatment_complete = _duplicate_totals(duplicate_records, "treatment")
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
    successful_control_rates = [
        rate
        for record in category_records["success"]
        if (rate := _duplicate_rate_for_record(record, "control")) is not None
    ]
    successful_treatment_rates = [
        rate
        for record in category_records["success"]
        if (rate := _duplicate_rate_for_record(record, "treatment")) is not None
    ]

    request_records = duplicate_records
    observed_treatment_requests = sum(len(_requests(record, "treatment")) for record in request_records)
    observed_control_requests = sum(len(_requests(record, "control")) for record in request_records)
    non_cache_treatment_requests = sum(len(_non_cache_requests(record, "treatment")) for record in request_records)
    non_cache_control_requests = sum(len(_non_cache_requests(record, "control")) for record in request_records)
    treatment_transport_failures = sum(_transport_failure_count(record, "treatment") for record in request_records)
    control_transport_failures = sum(_transport_failure_count(record, "control") for record in request_records)
    treatment_extraction_failures = sum(_extraction_failure_count(record, "treatment") for record in request_records)
    control_extraction_failures = sum(_extraction_failure_count(record, "control") for record in request_records)
    request_metrics_complete = all(
        isinstance(_arm(record, "treatment").get("requests"), list) for record in request_records
    )
    transport_failure_rate = _rate(treatment_transport_failures, non_cache_treatment_requests)
    extraction_failure_rate = _rate(treatment_extraction_failures, non_cache_treatment_requests)
    transport_pass = (
        complete_corpus
        and request_metrics_complete
        and non_cache_treatment_requests > 0
        and transport_failure_rate <= 0.02
    )
    extraction_quality_pass = (
        complete_corpus
        and request_metrics_complete
        and non_cache_treatment_requests > 0
        and extraction_failure_rate <= 0.05
    )
    failure_by_category = {
        category: {
            "case_count": len(category_records[category]),
            "control_non_cache_requests": sum(
                len(_non_cache_requests(record, "control")) for record in category_records[category]
            ),
            "treatment_non_cache_requests": sum(
                len(_non_cache_requests(record, "treatment")) for record in category_records[category]
            ),
            "control_transport_failures": sum(
                _transport_failure_count(record, "control") for record in category_records[category]
            ),
            "treatment_transport_failures": sum(
                _transport_failure_count(record, "treatment") for record in category_records[category]
            ),
            "control_extraction_failures": sum(
                _extraction_failure_count(record, "control") for record in category_records[category]
            ),
            "treatment_extraction_failures": sum(
                _extraction_failure_count(record, "treatment") for record in category_records[category]
            ),
        }
        for category in CASE_CATEGORIES
    }

    successful_residual_counts = [
        sum(
            event.get("outcome") == "claim_limit_residual_after_split"
            for event in _arm(record, "treatment").get("audit_events", [])
            if isinstance(event, Mapping)
        )
        for record in category_records["success"]
    ]
    cases_with_residual = sum(count > 0 for count in successful_residual_counts)

    gates = {
        "effective_output": {
            "status": "PASS" if effective_pass else "FAIL",
            "median_net_new_after_split": median_net_new,
            "cases_net_new_at_least_2": cases_at_least_two,
            "fraction_net_new_at_least_2": fraction_at_least_two,
            "thresholds": {"median_min": 3, "fraction_min": 0.5},
            "successful_cases": {
                "case_count": len(successful_net_new),
                "median_net_new_after_split": (
                    float(statistics.median(successful_net_new)) if successful_net_new else 0.0
                ),
                "cases_net_new_at_least_2": sum(value >= 2 for value in successful_net_new),
                "fraction_net_new_at_least_2": _rate(
                    sum(value >= 2 for value in successful_net_new),
                    len(successful_net_new),
                ),
            },
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
            "scored_case_count": len(duplicate_records),
            "excluded_runner_error_count": case_counts["runner_error"],
            "successful_case_medians": {
                "control_duplicate_rate": (
                    float(statistics.median(successful_control_rates)) if successful_control_rates else 0.0
                ),
                "treatment_duplicate_rate": (
                    float(statistics.median(successful_treatment_rates)) if successful_treatment_rates else 0.0
                ),
            },
        },
        "transport_failure": {
            "status": "PASS" if transport_pass else "FAIL",
            "observed_treatment_requests": observed_treatment_requests,
            "non_cache_treatment_request_count": non_cache_treatment_requests,
            "cached_treatment_request_count": observed_treatment_requests - non_cache_treatment_requests,
            "transport_failure_count": treatment_transport_failures,
            "failure_rate": transport_failure_rate,
            "threshold_max": 0.02,
            "metrics_complete": request_metrics_complete,
            "excluded_runner_error_count": case_counts["runner_error"],
            "all_arms": {
                "observed_request_count": observed_control_requests + observed_treatment_requests,
                "non_cache_request_count": non_cache_control_requests + non_cache_treatment_requests,
                "transport_failure_count": control_transport_failures + treatment_transport_failures,
            },
            "by_category": failure_by_category,
        },
        "extraction_quality_failure": {
            "status": "PASS" if extraction_quality_pass else "FAIL",
            "observed_treatment_requests": observed_treatment_requests,
            "non_cache_treatment_request_count": non_cache_treatment_requests,
            "cached_treatment_request_count": observed_treatment_requests - non_cache_treatment_requests,
            "extraction_failure_count": treatment_extraction_failures,
            "failure_rate": extraction_failure_rate,
            "threshold_max": 0.05,
            "metrics_complete": request_metrics_complete,
            "excluded_runner_error_count": case_counts["runner_error"],
            "all_arms": {
                "observed_request_count": observed_control_requests + observed_treatment_requests,
                "non_cache_request_count": non_cache_control_requests + non_cache_treatment_requests,
                "extraction_failure_count": control_extraction_failures + treatment_extraction_failures,
            },
            "by_category": failure_by_category,
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
        "classification": {
            "case_counts": case_counts,
            "control_root_not_compact_exact_20_count": sum(
                "control_root_not_compact_exact_20" in set(record.get("failure_reasons") or []) for record in observed
            ),
            "control_root_not_compact_exact_20_overlapping_api_error_count": sum(
                categories[str(record["case_id"])] == "api_error"
                and "control_root_not_compact_exact_20" in set(record.get("failure_reasons") or [])
                for record in observed
            ),
        },
        "diagnostics": {
            "residual_saturation": {
                "successful_case_count": len(successful_residual_counts),
                "cases_with_residual": cases_with_residual,
                "case_rate": _rate(cases_with_residual, len(successful_residual_counts)),
                "residual_event_count": sum(successful_residual_counts),
            }
        },
        "gates": gates,
    }


def _audit_events(record: Mapping[str, Any], arm: str) -> list[Mapping[str, Any]]:
    value = _arm(record, arm).get("audit_events")
    if not isinstance(value, list):
        return []
    return [event for event in value if isinstance(event, Mapping)]


def _is_runner_error(record: Mapping[str, Any]) -> bool:
    reasons = record.get("failure_reasons")
    return isinstance(reasons, list) and "runner_error" in reasons


def _repair_events(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [event for event in _audit_events(record, "treatment") if event.get("outcome") == "delta_repair_applied"]


def _repair_quality_failure_count(record: Mapping[str, Any]) -> int:
    failures = 0
    for event in _repair_events(record):
        detail = event.get("detail")
        if not isinstance(detail, Mapping) or detail.get("repair_status") != "failed":
            continue
        error_class = str(detail.get("error_class") or "").rsplit(".", 1)[-1]
        failures += error_class in EXTRACTION_QUALITY_ERROR_CLASSES
    return failures


def _index_v3_records(
    records: list[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    by_case: dict[str, Mapping[str, Any]] = {}
    duplicate_case_ids: list[str] = []
    for record in records:
        case_id = str(record.get("case_id") or "")
        if not case_id:
            raise ValueError("every run record must have a case_id")
        if case_id in by_case:
            duplicate_case_ids.append(case_id)
        by_case[case_id] = record
    return by_case, duplicate_case_ids


def score_v3_records(
    records: list[Mapping[str, Any]],
    baseline_records: list[Mapping[str, Any]],
    *,
    expected_case_count: int,
) -> dict[str, Any]:
    """Score a real P0+P1 arm against the frozen P0-only Flash baseline."""
    if expected_case_count < 1:
        raise ValueError("expected_case_count must be positive")
    treatment_by_case, treatment_duplicates = _index_v3_records(records)
    baseline_by_case, baseline_duplicates = _index_v3_records(baseline_records)
    matching_case_ids = set(treatment_by_case) == set(baseline_by_case)
    corpus_complete = (
        len(treatment_by_case) == expected_case_count
        and len(baseline_by_case) == expected_case_count
        and not treatment_duplicates
        and not baseline_duplicates
        and matching_case_ids
    )

    residual_case_ids = sorted(
        case_id
        for case_id, record in baseline_by_case.items()
        if any(
            event.get("outcome") == "claim_limit_residual_after_split" for event in _audit_events(record, "treatment")
        )
    )
    repair_net_new: list[int] = []
    repaired_case_ids: list[str] = []
    for case_id in residual_case_ids:
        treatment = treatment_by_case.get(case_id)
        events = _repair_events(treatment or {})
        if treatment is not None and not _is_runner_error(treatment) and events:
            repaired_case_ids.append(case_id)
        repair_net_new.append(
            sum(
                max(0, int(event.get("detail", {}).get("net_new_after_repair", 0)))
                for event in events
                if isinstance(event.get("detail"), Mapping)
            )
        )
    median_repair_net_new = float(statistics.median(repair_net_new)) if repair_net_new else 0.0
    repair_cases_at_least_two = sum(value >= 2 for value in repair_net_new)
    repair_fraction_at_least_two = _rate(repair_cases_at_least_two, len(residual_case_ids))
    repair_metrics_complete = len(repaired_case_ids) == len(residual_case_ids) and bool(residual_case_ids)
    repair_benefit_pass = (
        corpus_complete
        and repair_metrics_complete
        and median_repair_net_new >= 3
        and repair_fraction_at_least_two >= 0.5
    )

    baseline_scored_ids = [case_id for case_id, record in baseline_by_case.items() if not _is_runner_error(record)]
    duplicate_case_ids = [
        case_id
        for case_id in baseline_scored_ids
        if case_id in treatment_by_case and not _is_runner_error(treatment_by_case[case_id])
    ]
    baseline_duplicate_records = [baseline_by_case[case_id] for case_id in duplicate_case_ids]
    treatment_duplicate_records = [treatment_by_case[case_id] for case_id in duplicate_case_ids]
    baseline_claims, baseline_duplicate_count, baseline_duplicate_complete = _duplicate_totals(
        baseline_duplicate_records,
        "treatment",
    )
    treatment_claims, treatment_duplicate_count, treatment_duplicate_complete = _duplicate_totals(
        treatment_duplicate_records,
        "treatment",
    )
    baseline_duplicate_rate = _rate(baseline_duplicate_count, baseline_claims)
    treatment_duplicate_rate = _rate(treatment_duplicate_count, treatment_claims)
    duplicate_delta_pp = (treatment_duplicate_rate - baseline_duplicate_rate) * 100
    duplicate_metrics_complete = (
        baseline_duplicate_complete
        and treatment_duplicate_complete
        and len(duplicate_case_ids) == len(baseline_scored_ids)
    )
    duplicate_pass = (
        corpus_complete
        and duplicate_metrics_complete
        and baseline_claims > 0
        and treatment_claims > 0
        and duplicate_delta_pp <= 5.0
    )

    treatment_request_records = [record for record in treatment_by_case.values() if not _is_runner_error(record)]
    observed_treatment_requests = sum(len(_requests(record, "treatment")) for record in treatment_request_records)
    non_cache_treatment_requests = sum(
        len(_non_cache_requests(record, "treatment")) for record in treatment_request_records
    )
    transport_failures = sum(_transport_failure_count(record, "treatment") for record in treatment_request_records)
    request_metrics_complete = all(
        isinstance(_arm(record, "treatment").get("requests"), list) for record in treatment_request_records
    )
    transport_failure_rate = _rate(transport_failures, non_cache_treatment_requests)
    transport_pass = (
        corpus_complete
        and request_metrics_complete
        and non_cache_treatment_requests > 0
        and transport_failure_rate <= 0.02
    )

    repair_request_count = sum(len(_repair_events(record)) for record in treatment_request_records)
    extraction_failures = sum(_repair_quality_failure_count(record) for record in treatment_request_records)
    audit_metrics_complete = all(
        isinstance(_arm(record, "treatment").get("audit_events"), list) for record in treatment_request_records
    )
    extraction_failure_rate = _rate(extraction_failures, repair_request_count)
    extraction_quality_pass = (
        corpus_complete and audit_metrics_complete and repair_request_count > 0 and extraction_failure_rate <= 0.05
    )

    gates = {
        "repair_benefit": {
            "status": "PASS" if repair_benefit_pass else "FAIL",
            "baseline_residual_case_count": len(residual_case_ids),
            "repaired_case_count": len(repaired_case_ids),
            "median_net_new_after_repair": median_repair_net_new,
            "cases_net_new_at_least_2": repair_cases_at_least_two,
            "fraction_net_new_at_least_2": repair_fraction_at_least_two,
            "thresholds": {"median_min": 3, "fraction_min": 0.5},
            "metrics_complete": repair_metrics_complete,
        },
        "duplicate_pollution": {
            "status": "PASS" if duplicate_pass else "FAIL",
            "baseline_duplicate_rate": baseline_duplicate_rate,
            "treatment_duplicate_rate": treatment_duplicate_rate,
            "delta_pp": round(duplicate_delta_pp, 6),
            "threshold_delta_pp_max": 5.0,
            "baseline": {
                "claim_count": baseline_claims,
                "duplicate_count": baseline_duplicate_count,
            },
            "treatment": {
                "claim_count": treatment_claims,
                "duplicate_count": treatment_duplicate_count,
            },
            "scored_case_count": len(duplicate_case_ids),
            "metrics_complete": duplicate_metrics_complete,
        },
        "transport_failure": {
            "status": "PASS" if transport_pass else "FAIL",
            "observed_treatment_requests": observed_treatment_requests,
            "non_cache_treatment_request_count": non_cache_treatment_requests,
            "cached_treatment_request_count": observed_treatment_requests - non_cache_treatment_requests,
            "transport_failure_count": transport_failures,
            "failure_rate": transport_failure_rate,
            "threshold_max": 0.02,
            "metrics_complete": request_metrics_complete,
        },
        "extraction_quality_failure": {
            "status": "PASS" if extraction_quality_pass else "FAIL",
            "repair_request_count": repair_request_count,
            "extraction_failure_count": extraction_failures,
            "failure_rate": extraction_failure_rate,
            "threshold_max": 0.05,
            "metrics_complete": audit_metrics_complete,
        },
    }
    overall_pass = all(gate["status"] == "PASS" for gate in gates.values())
    return {
        "protocol_id": DELTA_REPAIR_PROTOCOL_ID,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "overall": "PASS" if overall_pass else "FAIL",
        "corpus": {
            "expected_case_count": expected_case_count,
            "baseline_case_count": len(baseline_by_case),
            "treatment_case_count": len(treatment_by_case),
            "baseline_duplicate_case_ids": sorted(set(baseline_duplicates)),
            "treatment_duplicate_case_ids": sorted(set(treatment_duplicates)),
            "matching_case_ids": matching_case_ids,
            "complete": corpus_complete,
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
    if manifest.get("protocol_id") != MANIFEST_PROTOCOL_ID:
        raise ValueError("manifest protocol_id does not match")
    report = score_records(_load_jsonl(runs_path), expected_case_count=int(manifest["case_count"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def score_v3_files(
    manifest_path: Path,
    runs_path: Path,
    baseline_runs_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol_id") != MANIFEST_PROTOCOL_ID:
        raise ValueError("manifest protocol_id does not match")
    report = score_v3_records(
        _load_jsonl(runs_path),
        _load_jsonl(baseline_runs_path),
        expected_case_count=int(manifest["case_count"]),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--baseline-runs", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = (
        score_v3_files(args.manifest, args.runs, args.baseline_runs, args.output)
        if args.baseline_runs is not None
        else score_files(args.manifest, args.runs, args.output)
    )
    print(json.dumps({"output": str(args.output), "overall": report["overall"]}, ensure_ascii=False))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
