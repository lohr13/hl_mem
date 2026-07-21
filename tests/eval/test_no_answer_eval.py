"""无答案评测测试。"""

from tests.eval.dataset import EvalCase
from tests.eval.metrics import aggregate_metrics, evaluate_results


def test_false_positive_reduces_no_answer_recall() -> None:
    case = EvalCase(
        case_id="N01", query="咖啡豆产地", intent="current_state", expected_type="empty",
        expected_min_confidence=None, expected_status_filter="active", expected_keywords=(), keyword_match="all",
        binding=None, forbidden_statuses=("disputed",),
    )
    score = evaluate_results(case, {"results": [{"id": "noise", "status": "active", "text": "咖啡", "evidence": []}]})

    assert aggregate_metrics([score])["no_answer_recall"] == 0.0
