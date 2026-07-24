"""离线召回指标测试。"""

from tests.eval.dataset import EvalCase
from tests.eval.metrics import (
    aggregate_metrics,
    compute_binary_ndcg_at_10,
    compute_mrr,
    evaluate_results,
)


def _case(**changes: object) -> EvalCase:
    values = dict(
        case_id="C01", query="偏好？", intent="current_state", expected_type="claim",
        expected_min_confidence=0.8, expected_status_filter="active", expected_keywords=("零基础设施",),
        keyword_match="all", binding=None, forbidden_statuses=("superseded", "expired", "disputed"),
        relevant_claim_ids=("right",), expected_evidence_event_ids=("event-1",),
    )
    values.update(changes)
    return EvalCase(**values)


def test_evaluate_results_scores_hit_content_status_and_evidence() -> None:
    result = {
        "results": [{
            "id": "right", "text": "偏好零基础设施", "status": "active", "confidence": 0.9,
            "valid_from": "2026-01-01T00:00:00+00:00", "evidence": [{"type": "event", "id": "event-1"}],
        }],
        "observations": [],
    }

    score = evaluate_results(_case(), result, latency_ms=12.5)

    assert score.recall_at_5 == 1.0
    assert score.top_1_correct == 1.0
    assert score.keyword_correct and score.confidence_correct and score.evidence_correct == 1.0
    assert score.stale_hits == 0
    assert score.latency_ms == 12.5


def test_rank_metrics_use_relevant_result_positions() -> None:
    results = [{"id": "noise"}, {"id": "right"}, {"id": "other-right"}]
    relevant = {"right", "other-right"}

    assert compute_mrr(relevant, results) == 0.5
    assert compute_binary_ndcg_at_10(relevant, results) == (
        1.0 / 1.584962500721156 + 1.0 / 2.0
    ) / (1.0 + 1.0 / 1.584962500721156)


def test_evaluate_results_populates_rank_metrics_only_for_claim_cases() -> None:
    claim_score = evaluate_results(
        _case(),
        {"results": [{"id": "noise"}, {"id": "right"}]},
    )
    empty_score = evaluate_results(
        _case(
            expected_type="empty",
            expected_min_confidence=None,
            expected_keywords=(),
            relevant_claim_ids=(),
            expected_evidence_event_ids=(),
        ),
        {"results": []},
    )

    assert claim_score.mrr == 0.5
    assert claim_score.ndcg_at_10 == 1.0 / 1.584962500721156
    assert empty_score.mrr is None
    assert empty_score.ndcg_at_10 is None


def test_evaluate_results_detects_stale_and_temporal_violations() -> None:
    case = _case(as_of="2026-02-01T00:00:00+00:00")
    result = {"results": [{
        "id": "wrong", "text": "无关", "status": "superseded", "confidence": 0.2,
        "valid_from": "2026-03-01T00:00:00+00:00", "valid_to": None, "evidence": [],
    }]}

    score = evaluate_results(case, result)

    assert score.recall_at_5 == 0.0
    assert score.top_1_correct == 0.0
    assert score.stale_hits == 1
    assert score.temporal_violations == 1


def test_aggregate_metrics_handles_answer_and_empty_cases() -> None:
    answer = evaluate_results(_case(), {"results": [{"id": "right", "text": "零基础设施", "status": "active", "confidence": 1.0, "evidence": [{"type": "event", "id": "event-1"}]}]})
    empty_true = evaluate_results(_case(case_id="N01", expected_type="empty", expected_min_confidence=None, expected_keywords=(), relevant_claim_ids=(), expected_evidence_event_ids=()), {"results": []})
    empty_false = evaluate_results(_case(case_id="N02", expected_type="empty", expected_min_confidence=None, expected_keywords=(), relevant_claim_ids=(), expected_evidence_event_ids=()), {"results": [{"id": "noise", "status": "active", "text": "x", "confidence": 1.0, "evidence": []}]})

    metrics = aggregate_metrics([answer, empty_true, empty_false])

    assert metrics["recall_at_5"] == 1.0
    assert metrics["micro_recall"] == 1.0
    assert metrics["top_1_correctness"] == 1.0
    assert metrics["no_answer_precision"] == 1.0
    assert metrics["no_answer_recall"] == 0.5
    assert metrics["evidence_correctness"] == 1.0
