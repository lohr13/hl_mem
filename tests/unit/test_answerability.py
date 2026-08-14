"""Answerability 的统一产品语义。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hl_mem.api.schemas import RecallOutput
from hl_mem.application.answerability import abstention_kind, is_abstention
from hl_mem.application.recall import RecallService
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer


@pytest.mark.parametrize(
    ("answerability", "expected_kind", "expected_abstention"),
    [
        ("supported", "none", False),
        ("no_evidence", "hard", True),
        ("low_confidence", "soft", True),
    ],
)
def test_answerability_has_one_hard_soft_classification(
    answerability: str,
    expected_kind: str,
    expected_abstention: bool,
) -> None:
    """交换 hard/soft 分支或漏算 soft abstention 时必须失败。"""
    assert abstention_kind(answerability) == expected_kind
    assert is_abstention(answerability) is expected_abstention


def test_recall_api_rejects_unknown_answerability() -> None:
    """API 若退回任意字符串，reader 将无法可靠执行拒答策略。"""
    with pytest.raises(ValidationError):
        RecallOutput(
            results=[],
            observations=[],
            policies=[],
            total=0,
            answerability="uncertain",
        )


@pytest.mark.parametrize(
    ("answerability", "results"),
    [
        ("no_evidence", [{"id": "claim-1", "text": "noise", "score": 0.1}]),
        ("low_confidence", []),
    ],
)
def test_recall_api_rejects_answerability_candidate_mismatches(
    answerability: str,
    results: list[dict[str, object]],
) -> None:
    """hard 携带候选或 soft 没有候选都违反公开语义。"""
    with pytest.raises(ValidationError):
        RecallOutput(
            results=results,
            observations=[],
            policies=[],
            total=len(results),
            answerability=answerability,
        )


def test_auxiliary_candidate_is_soft_not_hard_abstention() -> None:
    """Observation/Policy 候选存在时不能对外声称完全无证据。"""
    tracer = SearchTracer(
        SearchTrace(
            query_id="query-1",
            query_hash="hash",
            intent="current_state",
            limit=5,
            candidate_limit=10,
            candidates={},
            phases=SearchPhaseMetrics(),
        )
    )

    answerability = RecallService._answerability([], tracer, has_auxiliary_candidates=True)

    assert answerability == "low_confidence"
    assert tracer.trace.answerability == "low_confidence"
