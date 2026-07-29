"""recall_v2 runner 与发布门禁测试。"""

from tests.eval.eval_runner import _score
from tests.eval.gate_check import check


def test_score_consumes_answerability_and_reports_min_relevance() -> None:
    """answerability 驱动无答案诊断，min_relevance 仅进入诊断输出。"""
    row = {
        "id": "case-1",
        "slice": "no_answer",
        "expected_claim_ids": [],
        "equivalent_ids": [],
        "forbidden_ids": [],
        "min_relevance": "none",
    }

    score = _score(
        row,
        {"results": [{"id": "noise"}], "answerability": "no_evidence"},
        latency_ms=1.0,
        top_k=5,
    )

    assert score["predicted_no_answer"] is True
    assert score["low_confidence"] is False
    assert score["min_relevance_diagnostic"] == "not yet used for scoring"


def test_gate_checks_integrity_and_safety_metrics() -> None:
    """门禁拒绝样例分布变化、禁用命中及 HTTP 失败。"""
    metrics = {
        "mrr": 1.0,
        "recall_at_5": 1.0,
        "no_answer_precision": 1.0,
        "no_answer_recall": 1.0,
    }
    baseline = {
        "status": "ready",
        "dataset_sha256": "dataset",
        "snapshot_sha256": "snapshot",
        "case_count": 1,
        "slice_counts": {"no_answer": 1},
        "metrics": metrics,
        "slices": {},
    }
    report = {
        "artifacts": {"dataset_sha256": "dataset", "snapshot_sha256": "snapshot"},
        "case_count": 2,
        "slice_counts": {"no_answer": 2},
        "metrics": metrics,
        "slices": {},
        "total_forbidden_hits": 1,
        "http_success_rate": 0.5,
    }

    failures = check(report, baseline, tolerance=0.01, slice_tolerance=0.05)

    assert any("case_count" in failure for failure in failures)
    assert any("slice" in failure for failure in failures)
    assert any("forbidden" in failure for failure in failures)
    assert any("http_success_rate" in failure for failure in failures)
