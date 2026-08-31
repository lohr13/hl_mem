from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.release import core_v1
from benchmarks.release.compare_core_v1 import compare


def _protocol() -> dict[str, Any]:
    return {
        "max_metric_regression": 0.01,
        "required_forbidden_hits": 0,
        "required_http_success_rate": 1.0,
        "p95_limit": "max(baseline_ms + 150, baseline_ms * 1.25)",
        "required_external_model_calls": 0,
    }


def _result(*, recall_at_5: float = 0.95, forbidden_hits: int = 0, p95_ms: float = 20.0) -> dict[str, Any]:
    return {
        "metrics": {
            "recall_at_5": recall_at_5,
            "mrr": 0.9,
            "hard_abstention_precision": 1.0,
            "hard_abstention_recall": 1.0,
            "soft_abstention_precision": 1.0,
            "soft_abstention_recall": 1.0,
        },
        "total_forbidden_hits": forbidden_hits,
        "http_success_rate": 1.0,
        "external_model_calls": 0,
        "latency_ms": {"p50": 10.0, "p95": p95_ms},
        "dataset_sha256": "d" * 64,
        "protocol_sha256": "p" * 64,
        "case_count": 32,
    }


def test_comparator_rejects_regression_forbidden_hits_and_latency() -> None:
    baseline = _result()
    candidate = _result(recall_at_5=0.93, forbidden_hits=1, p95_ms=999.0)

    failures = compare(baseline, candidate, _protocol())

    assert any("recall_at_5" in item for item in failures)
    assert any("forbidden" in item for item in failures)
    assert any("p95" in item for item in failures)


def test_comparator_accepts_equal_functional_result_with_bounded_latency() -> None:
    assert compare(_result(), _result(p95_ms=169.0), _protocol()) == []


def test_runner_records_package_commit_and_protocol_hash(tmp_path: Path) -> None:
    output = tmp_path / "result.json"

    assert core_v1.main(["--label", "test", "--commit", "a" * 40, "--output", str(output)]) == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["package_version"]
    assert result["commit"] == "a" * 40
    assert len(result["protocol_sha256"]) == 64
    assert result["case_count"] == 32
    assert result["external_model_calls"] == 0
    assert result["http_success_rate"] == 1.0
    assert result["total_forbidden_hits"] == 0
