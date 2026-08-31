"""Compare two results produced by the frozen Core 1.0 public protocol."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROTOCOL = Path(__file__).with_name("core_v1_protocol.json")
GATED_METRICS = (
    "recall_at_5",
    "mrr",
    "hard_abstention_precision",
    "hard_abstention_recall",
    "soft_abstention_precision",
    "soft_abstention_recall",
)


def compare(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    for field in ("dataset_sha256", "protocol_sha256", "case_count"):
        if candidate.get(field) != baseline.get(field):
            failures.append(f"{field} differs from the baseline")

    tolerance = float(protocol["max_metric_regression"])
    baseline_metrics = baseline.get("metrics", {})
    candidate_metrics = candidate.get("metrics", {})
    for metric in GATED_METRICS:
        baseline_value = float(baseline_metrics.get(metric, 0.0))
        candidate_value = float(candidate_metrics.get(metric, 0.0))
        if candidate_value < baseline_value - tolerance:
            failures.append(
                f"{metric} regressed by {baseline_value - candidate_value:.6f} " f"(allowed {tolerance:.6f})"
            )

    required_forbidden = int(protocol["required_forbidden_hits"])
    if int(candidate.get("total_forbidden_hits", -1)) != required_forbidden:
        failures.append(
            f"forbidden hits must equal {required_forbidden}, got {candidate.get('total_forbidden_hits')!r}"
        )
    required_http = float(protocol["required_http_success_rate"])
    if float(candidate.get("http_success_rate", 0.0)) != required_http:
        failures.append(f"HTTP success rate must equal {required_http:.6f}, got {candidate.get('http_success_rate')!r}")
    required_calls = int(protocol["required_external_model_calls"])
    if int(candidate.get("external_model_calls", -1)) != required_calls:
        failures.append(
            f"external model calls must equal {required_calls}, got {candidate.get('external_model_calls')!r}"
        )

    baseline_p95 = float(baseline.get("latency_ms", {}).get("p95", 0.0))
    candidate_p95 = float(candidate.get("latency_ms", {}).get("p95", float("inf")))
    p95_limit = max(baseline_p95 + 150.0, baseline_p95 * 1.25)
    if candidate_p95 > p95_limit:
        failures.append(f"p95 latency {candidate_p95:.3f}ms exceeds frozen limit {p95_limit:.3f}ms")
    return failures


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    arguments = parser.parse_args(argv)
    try:
        failures = compare(_load(arguments.baseline), _load(arguments.candidate), _load(arguments.protocol))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Core 1.0 benchmark comparison failed: {error}")
        return 1
    if failures:
        print("Core 1.0 benchmark comparison failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Core 1.0 benchmark comparison passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
