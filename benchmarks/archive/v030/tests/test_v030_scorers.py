from __future__ import annotations

import pytest

from benchmarks.archive.v030.v030_scorers import evaluate_decision_gate, score_decisions, wilson_interval


def _case(case_id: str, decision: str, *, source: str = "local", winner: str | None = None) -> dict:
    gold = {"decision": decision}
    if winner is not None:
        gold["winner_candidate_key"] = winner
    return {"case_id": case_id, "source": source, "gold": gold}


def _prediction(case_id: str, decision: str, *, winner: str | None = None) -> dict:
    result = {"case_id": case_id, "decision": decision}
    if winner is not None:
        result["winner_candidate_key"] = winner
    return result


def test_wilson_interval_covers_empty_and_release_precision_boundary() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lower, upper = wilson_interval(100, 100)

    assert lower == pytest.approx(0.9630052, abs=1e-6)
    assert upper == 1.0


def test_score_decisions_reports_confusion_abstention_sources_and_destructive_errors() -> None:
    cases = [
        _case("a", "keep_left"),
        _case("b", "keep_right"),
        _case("c", "coexist", source="volcano"),
        _case("d", "select_candidate", winner="winner-a", source="volcano"),
        _case("e", "reject"),
    ]
    predictions = [
        _prediction("a", "keep_right"),
        _prediction("b", "manual_required"),
        _prediction("c", "coexist"),
        _prediction("d", "select_candidate", winner="winner-b"),
    ]

    report = score_decisions(cases, predictions)

    assert report["total"] == 5
    assert report["covered"] == 4
    assert report["exact"] == 1
    assert report["abstentions"] == 1
    assert report["destructive_error_case_ids"] == ["a", "d"]
    assert report["confusion"]["reject"]["<missing>"] == 1
    assert report["by_source"] == {
        "local": {"total": 3, "covered": 2, "exact": 0},
        "volcano": {"total": 2, "covered": 2, "exact": 1},
    }


def test_score_decisions_rejects_duplicate_and_unknown_predictions() -> None:
    cases = [_case("a", "coexist")]

    with pytest.raises(ValueError, match="duplicate prediction"):
        score_decisions(cases, [_prediction("a", "coexist"), _prediction("a", "coexist")])
    with pytest.raises(ValueError, match="unknown case"):
        score_decisions(cases, [_prediction("other", "coexist")])


def test_e1_gate_passes_exactly_at_preregistered_boundaries() -> None:
    report = {
        "total": 70,
        "exact": 67,
        "abstentions": 3,
        "destructive_error_case_ids": [],
        "invariant_violations": 0,
    }

    gate = evaluate_decision_gate(
        report,
        min_exact=67,
        max_abstentions=3,
        max_destructive=0,
        max_invariant_violations=0,
    )

    assert gate == {"passed": True, "failures": []}


@pytest.mark.parametrize(
    ("field", "value", "failure"),
    [
        ("exact", 66, "exact"),
        ("abstentions", 4, "abstentions"),
        ("destructive_error_case_ids", ["a"], "destructive"),
        ("invariant_violations", 1, "invariant"),
    ],
)
def test_e1_gate_fails_each_boundary_independently(field: str, value: object, failure: str) -> None:
    report = {
        "total": 70,
        "exact": 67,
        "abstentions": 3,
        "destructive_error_case_ids": [],
        "invariant_violations": 0,
    }
    report[field] = value

    gate = evaluate_decision_gate(
        report,
        min_exact=67,
        max_abstentions=3,
        max_destructive=0,
        max_invariant_violations=0,
    )

    assert gate["passed"] is False
    assert any(failure in item for item in gate["failures"])
