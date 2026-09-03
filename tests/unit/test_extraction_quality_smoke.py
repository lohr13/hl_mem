from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.tools.run_extraction_quality_smoke import load_cases, score_case
from hl_mem.ingest.extractors import ExtractedClaim

FIXTURE = Path("tests/eval/fixtures/extraction_quality_smoke_v1.json")


def test_fixture_is_versioned_and_fixed() -> None:
    cases = load_cases(FIXTURE)
    assert [case.case_id for case in cases] == [
        "attributed_viewpoint_and_speaker",
        "personal_reason_and_feeling",
        "named_relationship",
        "structured_event_content",
        "completed_decision_is_fact",
        "historical_pending_plan",
        "explicit_future_plan",
        "assistant_and_question_negatives",
    ]
    assert cases[0].messages[0]["text"].startswith("张岚：我认为真正的自由")


def test_score_case_requires_named_subject_terms_and_predicate() -> None:
    case = next(item for item in load_cases(FIXTURE) if item.case_id == "completed_decision_is_fact")
    passing = [
        ExtractedClaim(
            "事实",
            "李明已经完成原油、黄金和铜的配置",
            subject="李明",
        )
    ]
    wrong_kind = [
        ExtractedClaim(
            "计划",
            "李明将配置原油、黄金和铜",
            subject="李明",
        )
    ]
    wrong_subject = [
        ExtractedClaim(
            "事实",
            "用户已经完成原油、黄金和铜的配置",
            subject="user",
        )
    ]
    assert score_case(case, passing).passed is True
    assert score_case(case, wrong_kind).passed is False
    assert score_case(case, wrong_subject).passed is False


def test_score_case_requires_empty_output_for_negative_case() -> None:
    case = next(item for item in load_cases(FIXTURE) if item.case_id == "assistant_and_question_negatives")
    assert score_case(case, []).passed is True
    assert score_case(case, [ExtractedClaim("事实", "AI 助手喜欢散步", subject="AI 助手")]).passed is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 2, "cases": []}, "schema_version"),
        (
            {
                "schema_version": 1,
                "cases": [
                    {
                        "id": "same",
                        "occurred_at": "now",
                        "messages": [],
                        "required_claims": [],
                        "forbidden_subjects": [],
                        "expect_empty": False,
                    },
                    {
                        "id": "same",
                        "occurred_at": "later",
                        "messages": [],
                        "required_claims": [],
                        "forbidden_subjects": [],
                        "expect_empty": False,
                    },
                ],
            },
            "duplicate case id",
        ),
        (
            {
                "schema_version": 1,
                "cases": [
                    {
                        "id": "empty-group",
                        "occurred_at": "now",
                        "messages": [],
                        "required_claims": [{"subject": "name", "term_groups": [[]]}],
                        "forbidden_subjects": [],
                        "expect_empty": False,
                    }
                ],
            },
            "empty term group",
        ),
        (
            {
                "schema_version": 1,
                "cases": [
                    {
                        "id": "negative",
                        "occurred_at": "now",
                        "messages": [],
                        "required_claims": [{"subject": "name", "term_groups": [["term"]]}],
                        "forbidden_subjects": [],
                        "expect_empty": True,
                    }
                ],
            },
            "empty case cannot require claims",
        ),
    ],
)
def test_load_cases_rejects_invalid_shapes(tmp_path: Path, payload: dict[str, object], message: str) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_cases(path)
