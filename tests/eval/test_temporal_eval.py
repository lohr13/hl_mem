"""双时间评测诊断测试。"""

from tests.eval.dataset import EvalCase
from tests.eval.metrics import evaluate_results


def test_historical_result_outside_valid_interval_is_counted() -> None:
    case = EvalCase(
        case_id="T01", query="过去状态", intent="historical", expected_type="claim",
        expected_min_confidence=0.9, expected_status_filter="all", expected_keywords=("失败",), keyword_match="all",
        binding=None, forbidden_statuses=("disputed",), as_of="2026-01-15T00:00:00+00:00",
        relevant_claim_ids=("old",),
    )
    response = {"results": [{
        "id": "old", "text": "执行失败", "status": "superseded", "confidence": 0.95,
        "valid_from": "2026-02-01T00:00:00+00:00", "valid_to": None, "evidence": [],
    }]}

    assert evaluate_results(case, response).temporal_violations == 1
