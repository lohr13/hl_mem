from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from evaluation.tools import run_extraction_quality_smoke as smoke
from evaluation.tools.run_extraction_quality_smoke import ExpectedClaim, SmokeCase, load_cases, score_case
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.observability.audit import current_audit
from hl_mem.settings import Settings

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


class _FakeExtractor:
    def __init__(self, claims: list[ExtractedClaim], *, llm_calls: int, retained_count: int) -> None:
        self.claims = claims
        self.last_llm_call_count = llm_calls
        self.last_input_tokens = 12
        self.last_output_tokens = 7
        self.retained_count = retained_count
        self.contents: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.llm_client = self
        self.closed = False

    def extract(self, content: dict[str, Any], context: dict[str, Any]) -> list[ExtractedClaim]:
        self.contents.append((content, context))
        if self.retained_count != len(self.claims):
            current_audit().emit(
                "extract",
                "claim_budget",
                "overflow_truncated",
                detail={"generated_claim_count": self.retained_count + 1, "retained_claim_count": self.retained_count},
            )
        return self.claims

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("llm_calls", "retained_count", "expected_exit"),
    [(1, 1, 0), (2, 1, 1), (1, 17, 1)],
)
def test_run_writes_safe_fixed_fixture_report_and_enforces_execution_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_calls: int,
    retained_count: int,
    expected_exit: int,
) -> None:
    case = SmokeCase(
        case_id="fixed-case",
        occurred_at="2026-09-03T00:00:00+00:00",
        messages=(
            {"speaker": "user", "text": "first fixed source"},
            {"speaker": "assistant", "text": "second fixed source"},
        ),
        required_claims=(ExpectedClaim(subject="person", term_groups=(("safe",),)),),
        forbidden_subjects=frozenset(),
        expect_empty=False,
    )
    extractor = _FakeExtractor(
        [ExtractedClaim("fact", "safe synthetic value", subject="person")],
        llm_calls=llm_calls,
        retained_count=retained_count,
    )
    captured_settings: list[Settings] = []
    loaded_paths: list[Path] = []

    def load_fixed_case(path: Path) -> tuple[SmokeCase, ...]:
        loaded_paths.append(path)
        return (case,)

    monkeypatch.setattr(smoke, "load_cases", load_fixed_case)
    monkeypatch.setattr(smoke, "load_settings", lambda *_args: Settings.for_test())

    def make_fake_extractor(settings: Settings, *, require_real: bool) -> _FakeExtractor:
        assert require_real is True
        captured_settings.append(settings)
        return extractor

    monkeypatch.setattr(smoke, "make_extractor", make_fake_extractor)
    report_path = tmp_path / "report.json"
    args = argparse.Namespace(
        config=tmp_path / "config.toml",
        env_file=tmp_path / ".env",
        label="offline-test",
        report=report_path,
    )

    assert smoke.run(args) == expected_exit

    assert loaded_paths == [smoke.FIXTURE_PATH]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert set(report) == {"schema_version", "label", "generated_at", "model", "summary", "cases"}
    assert report["schema_version"] == "extraction-quality-smoke-v1"
    assert report["summary"]["llm_calls"] == llm_calls
    result = report["cases"][0]
    assert result["generated_count"] == retained_count + (retained_count != 1)
    assert result["retained_count"] == retained_count
    assert result["input_tokens"] == 12
    assert result["output_tokens"] == 7
    assert result["latency_ms"] >= 0
    assert result["claim_summaries"] == [{"subject": "person", "predicate": "fact", "value": "safe synthetic value"}]
    assert captured_settings[0].verification_mode == "off"
    assert captured_settings[0].llm_schema_retries == 0
    assert captured_settings[0].llm_max_attempts == 1
    assert extractor.closed is True
    content, context = extractor.contents[0]
    assert content["messages"] == [
        {
            "event_index": 0,
            "speaker": "user",
            "turn": 0,
            "occurred_at": case.occurred_at,
            "content": "first fixed source",
        },
        {
            "event_index": 1,
            "speaker": "assistant",
            "turn": 1,
            "occurred_at": case.occurred_at,
            "content": "second fixed source",
        },
    ]
    assert context["_source_events"][1] == {
        "id": "extraction-quality-smoke:fixed-case:1",
        "actor_type": "assistant",
        "content": {"text": "second fixed source"},
        "occurred_at": case.occurred_at,
    }


def test_cli_rejects_an_arbitrary_fixture_override() -> None:
    with pytest.raises(SystemExit):
        smoke.parse_args(
            [
                "--config",
                "config.toml",
                "--env-file",
                ".env",
                "--label",
                "label",
                "--report",
                "report.json",
                "--fixture",
                "untrusted.json",
            ]
        )
