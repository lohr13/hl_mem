from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evaluation.tools import run_chinese_reader_replay as replay
from evaluation.tools.run_memdaily_benchmark import MemDailyMessage, MemDailyTrajectory
from tests.eval.chinese_e2e import (
    AnswerEntityGold,
    E2EQuestion,
    E2ESampleManifest,
    PerLTQABundle,
    SampledInputs,
)


def make_gold(case_id: str) -> AnswerEntityGold:
    return AnswerEntityGold(
        answerability="answerable",
        answer_entities=(f"entity-{case_id}",),
        role_action_object=(),
        forbidden_entities=(),
        forbidden_assertions=(),
    )


def make_trajectory(
    *,
    case_id: str,
    choices: dict[str, str] | None = None,
) -> MemDailyTrajectory:
    return MemDailyTrajectory(
        case_id=case_id,
        qtype=case_id.split(":")[1],
        subtype="events",
        tid=1,
        namespace=f"namespace-{case_id}",
        question=f"question-{case_id}",
        answer=f"answer-{case_id}",
        question_at="2026-01-01T00:00:00+00:00",
        ground_truth_choice="A" if choices else None,
        choices=choices or {},
        messages=(
            MemDailyMessage(
                mid=1,
                event_id=f"event-{case_id}",
                occurred_at="2025-01-01T00:00:00+00:00",
                text=f"synthetic-message-{case_id}",
                place="synthetic-place",
            ),
        ),
        gold_event_ids=(f"event-{case_id}",),
    )


def make_inputs(first: MemDailyTrajectory | None = None) -> SampledInputs:
    trajectories = [first] if first is not None else []
    trajectories.extend(
        make_trajectory(case_id=f"memdaily:simple:events:{index}")
        for index in range(2 if first is not None else 1, 41)
    )
    return SampledInputs(perltqa_bundles=(), memdaily_trajectories=tuple(trajectories))


def make_manifest(first: MemDailyTrajectory | None = None) -> E2ESampleManifest:
    inputs = make_inputs(first)
    case_ids = [trajectory.case_id for trajectory in inputs.memdaily_trajectories]
    return E2ESampleManifest(
        schema_version=3,
        sample_id="synthetic-reader-replay",
        sources={
            name: {"path": f"synthetic-{name}.json", "sha256": "0" * 64}
            for name in ("perltqa_memory", "perltqa_qa", "memdaily")
        },
        perltqa={
            "personas": [],
            "expected_personas": 0,
            "expected_questions": 0,
            "evaluation_as_of": "2026-01-01T00:00:00+00:00",
        },
        memdaily={"case_ids": case_ids, "expected_questions": len(case_ids)},
        accepted_rubrics_by_question_hash={},
        answer_entity_scorer_version="answer-entity-packet-v1",
        answer_entity_gold_by_case_id={case_id: make_gold(case_id) for case_id in case_ids},
    )


def make_case(
    case_id: str,
    *,
    retrieved: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "dataset": "memdaily",
        "slice": "memdaily_simple",
        "case_id": case_id,
        "question": f"question-{case_id}",
        "answer": f"answer-{case_id}",
        "retrieved": retrieved if retrieved is not None else [{"rank": 1, "text": "synthetic evidence"}],
        "qa": {
            "model": "qwen3.7-plus",
            "gold_answer": f"answer-{case_id}",
            "answerability": "supported",
        },
        "error": None,
    }


def make_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "benchmark": "chinese_e2e",
        "scorer_version": "deterministic-rubric-v2",
        "answer_entity_scorer_version": "answer-entity-packet-v1",
        "status": "completed",
        "sample": {"id": "zh-e2e-v3"},
        "run": {"models": {"extractor": "synthetic-extractor", "qa": "qwen3.7-plus"}},
        "cases": cases,
    }


def write_report(tmp_path: Path, cases: list[dict[str, Any]]) -> Path:
    path = tmp_path / "source-report.json"
    path.write_text(json.dumps(make_report(cases)), encoding="utf-8")
    return path


def all_cases(first: MemDailyTrajectory) -> list[dict[str, Any]]:
    return [make_case(trajectory.case_id) for trajectory in make_inputs(first).memdaily_trajectories]


def test_validate_source_report_preserves_all_ten_reader_items() -> None:
    case = make_case("case-1", retrieved=[{"rank": rank, "text": str(rank)} for rank in range(1, 11)])
    report = make_report([case])
    validated = replay.validate_source_report(report, expected_case_ids={"case-1"})
    assert [item["rank"] for item in validated[0]["retrieved"]] == list(range(1, 11))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(schema_version=2), "schema_version"),
        (lambda report: report.update(status="partial"), "status"),
        (lambda report: report["cases"][0].update(error="boom"), "case errors"),
        (lambda report: report["cases"][0]["qa"].update(model="glm-5.3-flash"), "qwen3.7-plus"),
    ],
)
def test_validate_source_report_rejects_nonofficial_inputs(mutation: Any, message: str) -> None:
    report = make_report([make_case("case-1")])
    mutation(report)
    with pytest.raises(replay.ReplayInputError, match=message):
        replay.validate_source_report(report, expected_case_ids={"case-1"})


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(benchmark="other"), "benchmark"),
        (lambda report: report.update(scorer_version="other"), "scorer_version"),
        (
            lambda report: report.update(answer_entity_scorer_version="other"),
            "answer_entity_scorer_version",
        ),
        (lambda report: report["sample"].update(id="other"), "sample.id"),
        (lambda report: report["run"]["models"].update(qa="other"), "run.models.qa"),
    ],
)
def test_validate_source_report_rejects_wrong_official_identity(mutation: Any, message: str) -> None:
    report = make_report([make_case("case-1")])
    mutation(report)
    with pytest.raises(replay.ReplayInputError, match=message):
        replay.validate_source_report(report, expected_case_ids={"case-1"})


@pytest.mark.parametrize(
    ("report", "expected_case_ids", "message"),
    [
        ([], {"case-1"}, "object"),
        (make_report([make_case("case-1"), make_case("case-1")]), {"case-1"}, "duplicate"),
        (make_report([]), {"case-1"}, "missing"),
        (make_report([make_case("case-2")]), {"case-1"}, "unexpected"),
    ],
)
def test_validate_source_report_rejects_invalid_case_sets(
    report: object,
    expected_case_ids: set[str],
    message: str,
) -> None:
    with pytest.raises(replay.ReplayInputError, match=message):
        replay.validate_source_report(report, expected_case_ids=expected_case_ids)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda case: case.update(qa=None), "QA"),
        (lambda case: case["qa"].update(answerability="hard_abstention"), "answerability"),
        (lambda case: case.update(retrieved={"rank": 1}), "retrieved"),
        (lambda case: case.update(retrieved=["not-an-object"]), "retrieved"),
    ],
)
def test_validate_source_report_rejects_invalid_reader_payloads(mutation: Any, message: str) -> None:
    case = make_case("case-1")
    mutation(case)
    with pytest.raises(replay.ReplayInputError, match=message):
        replay.validate_source_report(make_report([case]), expected_case_ids={"case-1"})


def test_load_replay_cases_joins_memdaily_choices_without_copying_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trajectory = make_trajectory(case_id="memdaily:simple:events:1", choices={"A": "left", "B": "right"})
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: make_manifest(trajectory))
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda manifest: make_inputs(trajectory))
    source_cases = all_cases(trajectory)
    source_cases[0]["messages"] = ["synthetic source message"]
    source = write_report(tmp_path, source_cases)
    loaded = replay.load_replay_cases(tmp_path / "manifest.json", {"qwen37": source})

    assert loaded["qwen37"][0].trajectory.choices == {"A": "left", "B": "right"}
    assert loaded["qwen37"][0].retrieved == tuple(make_case(trajectory.case_id)["retrieved"])
    assert loaded["qwen37"][0].trajectory.messages == ()
    assert not hasattr(loaded["qwen37"][0], "messages")
    assert "messages" not in loaded["qwen37"][0].source_case
    assert loaded["qwen37"][0].answer_entity_gold == make_gold(trajectory.case_id)
    assert loaded["qwen37"][0].arm.report_sha256 == replay.sha256_file(source)
    assert [case.case_id for case in loaded["qwen37"]] == [case["case_id"] for case in source_cases]


def test_build_trajectory_index_reconstructs_perltqa_metadata_with_one_ingest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_gold = make_gold("perltqa:synthetic:dialogues:1")
    second_gold = make_gold("perltqa:synthetic:dialogues:2")
    accepted_rubrics = ((("rubric-a", "rubric-b"),),)
    questions = (
        E2EQuestion(
            case_id="perltqa:synthetic:dialogues:1",
            category="dialogues",
            question="synthetic PerLTQA question 1",
            answer="synthetic PerLTQA answer 1",
            answer_anchors=("anchor-1",),
            accepted_rubrics=accepted_rubrics,
            answer_entity_gold=first_gold,
            namespace="synthetic-perltqa",
            gold_event_ids=("perltqa-event-1",),
        ),
        E2EQuestion(
            case_id="perltqa:synthetic:dialogues:2",
            category="dialogues",
            question="synthetic PerLTQA question 2",
            answer="synthetic PerLTQA answer 2",
            answer_anchors=("anchor-2",),
            accepted_rubrics=accepted_rubrics,
            answer_entity_gold=second_gold,
            namespace="synthetic-perltqa",
            gold_event_ids=("perltqa-event-2",),
        ),
    )
    bundle = PerLTQABundle(
        name="synthetic persona",
        namespace="synthetic-perltqa",
        evaluation_as_of="2026-01-01T00:00:00+00:00",
        messages=make_trajectory(case_id="memdaily:simple:events:99").messages,
        questions=questions,
    )
    memdaily = make_inputs().memdaily_trajectories[:38]
    manifest = replace(
        make_manifest(),
        memdaily={"case_ids": [trajectory.case_id for trajectory in memdaily], "expected_questions": 38},
        answer_entity_gold_by_case_id={
            trajectory.case_id: make_gold(trajectory.case_id) for trajectory in memdaily
        },
    )
    sampled = SampledInputs(perltqa_bundles=(bundle,), memdaily_trajectories=memdaily)
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: manifest)
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda loaded_manifest: sampled)
    real_build_ingest = replay.build_perltqa_ingest_trajectory
    ingest_names: list[str] = []

    def observe_build_ingest(observed_bundle: PerLTQABundle) -> MemDailyTrajectory:
        ingest_names.append(observed_bundle.name)
        return real_build_ingest(observed_bundle)

    monkeypatch.setattr(replay, "build_perltqa_ingest_trajectory", observe_build_ingest)

    index = replay.build_trajectory_index(tmp_path / "manifest.json")

    assert ingest_names == ["synthetic persona"]
    assert len(index) == 40
    reconstructed = index[questions[0].case_id]
    assert reconstructed.trajectory.question == "synthetic PerLTQA question 1"
    assert reconstructed.answer_anchors == ("anchor-1",)
    assert reconstructed.accepted_rubrics == ((("rubric-a", "rubric-b"),),)
    assert reconstructed.answer_entity_gold == first_gold


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("question", "wrong question", "question"),
        ("answer", "wrong answer", "gold answer"),
    ],
)
def test_load_replay_cases_rejects_report_text_that_does_not_match_trajectory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    replacement: str,
    message: str,
) -> None:
    trajectory = make_trajectory(case_id="memdaily:simple:events:1")
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: make_manifest(trajectory))
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda manifest: make_inputs(trajectory))
    cases = all_cases(trajectory)
    cases[0][field] = replacement
    if field == "answer":
        cases[0]["qa"]["gold_answer"] = replacement
    source = write_report(tmp_path, cases)

    with pytest.raises(replay.ReplayInputError, match=message):
        replay.load_replay_cases(tmp_path / "manifest.json", {"qwen37": source})


def test_validate_source_report_returns_copies() -> None:
    case = make_case("case-1")
    original = deepcopy(case)
    [validated] = replay.validate_source_report(make_report([case]), expected_case_ids={"case-1"})

    assert validated == original
    assert validated is not case
    assert validated["qa"] is not case["qa"]
    assert validated["retrieved"] is not case["retrieved"]
    assert validated["retrieved"][0] is not case["retrieved"][0]
