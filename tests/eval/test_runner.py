"""评测 runner 测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.eval.dataset import EvalCase
from tests.eval.metrics import evaluate_results
from tests.eval.runner import (
    compute_latency_percentiles,
    print_report_summary,
    run_evaluation,
    write_report,
)


def test_runner_records_per_case_metrics_and_manifest(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    sqlite3.connect(database_path).close()
    case = EvalCase(
        case_id="N01", query="不存在？", intent="current_state", expected_type="empty",
        expected_min_confidence=None, expected_status_filter="active", expected_keywords=(), keyword_match="all",
        binding=None, forbidden_statuses=("disputed",),
    )

    report = run_evaluation([case], lambda _case: {"results": [], "observations": []}, database_path)
    output = tmp_path / "report.json"
    write_report(report, output)

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["manifest"]["source_sha256"]
    assert persisted["manifest"]["case_count"] == 1
    assert persisted["queries"][0]["case_id"] == "N01"
    assert persisted["metrics"]["no_answer_recall"] == 1.0
    assert persisted["test_layer"] == {"passed": 1, "failed": 0, "skipped": 0}
    assert persisted["metrics"]["mrr"] == 0.0
    assert persisted["metrics"]["ndcg_at_10"] == 0.0
    assert persisted["latency"]["p50"] >= 0.0
    assert persisted["latency"]["p95"] >= 0.0


def test_latency_percentiles_handle_multiple_scores() -> None:
    case = EvalCase(
        case_id="N01", query="不存在？", intent="current_state", expected_type="empty",
        expected_min_confidence=None, expected_status_filter="active", expected_keywords=(), keyword_match="all",
        binding=None, forbidden_statuses=("disputed",),
    )
    scores = [
        evaluate_results(case, {"results": []}, latency_ms=10.0),
        evaluate_results(case, {"results": []}, latency_ms=20.0),
    ]

    assert compute_latency_percentiles(scores) == {"p50": 15.0, "p95": 28.5}


def test_report_summary_separates_test_and_retrieval_metrics(capsys: pytest.CaptureFixture[str]) -> None:
    report = {
        "test_layer": {"passed": 2, "failed": 1, "skipped": 3},
        "metrics": {"recall_at_5": 0.5, "mrr": 0.25, "ndcg_at_10": 0.375},
        "latency": {"p50": 12.34, "p95": 56.78},
    }

    print_report_summary(report)

    output = capsys.readouterr().out
    assert "Test layer: passed=2, failed=1, skipped=3" in output
    assert "Retrieval: recall@5=0.500, MRR=0.250, nDCG@10=0.375" in output
    assert "Latency: p50=12.3ms, p95=56.8ms" in output


def test_report_summary_prints_optional_scenarios_layer(capsys: pytest.CaptureFixture[str]) -> None:
    """报告包含 scenarios 统计时应单独输出行为场景层。"""
    report = {
        "test_layer": {"passed": 2, "failed": 0, "skipped": 0},
        "scenarios": {"passed": 29, "failed": 1, "skipped": 0},
        "metrics": {"recall_at_5": 1.0, "mrr": 1.0, "ndcg_at_10": 1.0},
        "latency": {"p50": 10.0, "p95": 20.0},
    }

    print_report_summary(report)

    output = capsys.readouterr().out
    assert "Scenarios: passed=29, failed=1, skipped=0" in output
