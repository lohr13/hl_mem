from __future__ import annotations

import dataclasses
import json
import os
import types
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
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

BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
MODEL = "glm-5.3-flash"


class FakeThinkingTransport:
    def __init__(
        self,
        *,
        answer: str,
        verified: bool,
        fail_on_calls: set[int] | None = None,
    ) -> None:
        self.answer = answer
        self.verified = verified
        self.fail_on_calls = fail_on_calls or set()
        self.calls: list[tuple[str, str, str, str, str]] = []
        self.last_call: replay.ReaderCallMetadata | None = None

    def __call__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, int]:
        self.calls.append((api_key, base_url, model, system_prompt, user_prompt))
        call_number = len(self.calls)
        self.last_call = None
        if call_number in self.fail_on_calls:
            self.last_call = replay.ReaderCallMetadata(
                input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                total_tokens=0,
                latency_seconds=0.5,
                attempts=3,
                thinking_verified=False,
            )
            raise replay.RetryExhaustedTransientError(self.last_call)
        self.last_call = replay.ReaderCallMetadata(
            input_tokens=10,
            output_tokens=7,
            reasoning_tokens=5,
            total_tokens=17,
            latency_seconds=0.25,
            attempts=2,
            thinking_verified=self.verified,
        )
        return self.answer, 17


def success_envelope() -> dict[str, Any]:
    return {
        "choices": [{"message": {"reasoning_content": "private chain", "content": "answer"}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 7,
            "total_tokens": 17,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }


def mock_client(
    actions: list[dict[str, Any] | httpx.Response | BaseException],
) -> tuple[httpx.Client, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        action = actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, httpx.Response):
            return httpx.Response(
                action.status_code,
                request=request,
                headers=action.headers,
                content=action.content,
            )
        return httpx.Response(200, json=action)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def _traceback_referents(error: BaseException) -> list[object]:
    roots: list[object] = [error]
    pending_errors: list[BaseException] = [error]
    seen_errors: set[int] = set()
    while pending_errors:
        current_error = pending_errors.pop()
        if id(current_error) in seen_errors:
            continue
        seen_errors.add(id(current_error))
        traceback = current_error.__traceback__
        while traceback is not None:
            module_name = str(traceback.tb_frame.f_globals.get("__name__", ""))
            if module_name in {
                replay.__name__,
                "evaluation.tools.longmemeval.qa_client",
                "hl_mem.http_utils",
            }:
                roots.extend(traceback.tb_frame.f_locals.values())
            traceback = traceback.tb_next
        pending_errors.extend(item for item in (current_error.__cause__, current_error.__context__) if item is not None)

    referents: list[object] = []
    pending = [(root, 8) for root in roots]
    seen: set[int] = set()
    while pending:
        value, depth = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        referents.append(value)
        if depth == 0 or isinstance(value, (str, bytes, bytearray, int, float, bool, type(None))):
            continue
        children: list[object] = []
        if isinstance(value, dict):
            children.extend(value.keys())
            children.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            children.extend(value)
        elif isinstance(value, BaseException):
            children.extend(value.args)
            children.extend(item for item in (value.__cause__, value.__context__) if item is not None)
            children.extend(vars(value).values())
        elif isinstance(value, types.FunctionType):
            for cell in value.__closure__ or ():
                try:
                    children.append(cell.cell_contents)
                except ValueError:
                    pass
        elif not isinstance(value, (type, types.ModuleType)):
            try:
                children.extend(vars(value).values())
            except TypeError:
                pass
        pending.extend((child, depth - 1) for child in children)
    return referents


def assert_transport_exception_drops(
    error: BaseException,
    *,
    strings: tuple[str, ...],
    objects: tuple[object, ...] = (),
) -> None:
    referents = _traceback_referents(error)
    for value in referents:
        assert all(value is not forbidden for forbidden in objects)
        if isinstance(value, str):
            assert all(forbidden not in value for forbidden in strings)
        elif isinstance(value, (bytes, bytearray)):
            assert all(forbidden.encode() not in value for forbidden in strings)


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
        make_trajectory(case_id=f"memdaily:simple:events:{index}") for index in range(2 if first is not None else 1, 41)
    )
    return SampledInputs(perltqa_bundles=(), memdaily_trajectories=tuple(trajectories))


def make_manifest(first: MemDailyTrajectory | None = None) -> E2ESampleManifest:
    inputs = make_inputs(first)
    case_ids = [trajectory.case_id for trajectory in inputs.memdaily_trajectories]
    return E2ESampleManifest(
        schema_version=3,
        sample_id="zh-e2e-v3",
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


def make_report(
    cases: list[dict[str, Any]],
    *,
    extractor_model: str = "qwen3.7-plus",
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "benchmark": "chinese_e2e",
        "scorer_version": "deterministic-rubric-v2",
        "answer_entity_scorer_version": "answer-entity-packet-v1",
        "status": "completed",
        "sample": {
            "id": "zh-e2e-v3",
            "sources": make_manifest().sources,
            "perltqa_questions": 0,
            "memdaily_questions": 40,
            "perltqa_evaluation_as_of": "2026-01-01T00:00:00+00:00",
            "slice_counts": {"memdaily_simple": 40},
        },
        "run": {"models": {"extractor": extractor_model, "qa": "qwen3.7-plus"}},
        "cases": cases,
    }


def write_report(
    tmp_path: Path,
    cases: list[dict[str, Any]],
    *,
    extractor_model: str = "qwen3.7-plus",
) -> Path:
    path = tmp_path / "source-report.json"
    path.write_text(json.dumps(make_report(cases, extractor_model=extractor_model)), encoding="utf-8")
    return path


def write_manifest_placeholder(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text('{"synthetic":"manifest"}\n', encoding="utf-8")
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
    manifest_path = write_manifest_placeholder(tmp_path)
    source_cases = all_cases(trajectory)
    source_cases[0]["messages"] = ["synthetic source message"]
    source = write_report(tmp_path, source_cases)
    loaded = replay.load_replay_cases(manifest_path, {"qwen37": source})

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
        answer_entity_gold_by_case_id={trajectory.case_id: make_gold(trajectory.case_id) for trajectory in memdaily},
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
    manifest_path = write_manifest_placeholder(tmp_path)
    cases = all_cases(trajectory)
    cases[0][field] = replacement
    if field == "answer":
        cases[0]["qa"]["gold_answer"] = replacement
    source = write_report(tmp_path, cases)

    with pytest.raises(replay.ReplayInputError, match=message):
        replay.load_replay_cases(manifest_path, {"qwen37": source})


def test_load_replay_cases_rejects_swapped_extractor_arm_before_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trajectory = make_trajectory(case_id="memdaily:simple:events:1")
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: make_manifest(trajectory))
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda manifest: make_inputs(trajectory))
    manifest_path = write_manifest_placeholder(tmp_path)
    source = write_report(tmp_path, all_cases(trajectory), extractor_model="glm-5.3-flash")

    with pytest.raises(replay.ReplayInputError, match="extractor"):
        replay.load_replay_cases(manifest_path, {"qwen37": source})


def test_load_replay_cases_rejects_manifest_source_declaration_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trajectory = make_trajectory(case_id="memdaily:simple:events:1")
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: make_manifest(trajectory))
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda manifest: make_inputs(trajectory))
    manifest_path = write_manifest_placeholder(tmp_path)
    report = make_report(all_cases(trajectory))
    report["sample"]["sources"]["memdaily"]["sha256"] = "f" * 64
    source = tmp_path / "source-report.json"
    source.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(replay.ReplayInputError, match="sample declarations"):
        replay.load_replay_cases(manifest_path, {"qwen37": source})


def test_load_replay_cases_binds_manifest_and_scoring_input_digests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trajectory = make_trajectory(case_id="memdaily:simple:events:1")
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: make_manifest(trajectory))
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda manifest: make_inputs(trajectory))
    manifest_path = write_manifest_placeholder(tmp_path)
    source = write_report(tmp_path, all_cases(trajectory))

    loaded = replay.load_replay_cases(manifest_path, {"qwen37": source})
    arm = loaded["qwen37"][0].arm

    assert arm.manifest_sha256 == replay.sha256_file(manifest_path)
    assert len(arm.scoring_inputs_sha256) == 64


def test_validate_source_report_returns_copies() -> None:
    case = make_case("case-1")
    original = deepcopy(case)
    [validated] = replay.validate_source_report(make_report([case]), expected_case_ids={"case-1"})

    assert validated == original
    assert validated is not case
    assert validated["qa"] is not case["qa"]
    assert validated["retrieved"] is not case["retrieved"]
    assert validated["retrieved"][0] is not case["retrieved"][0]


def test_glm_payload_uses_provider_thinking_object() -> None:
    payload = replay.build_glm_thinking_payload(MODEL, "system", "user")

    assert payload == {
        "model": "glm-5.3-flash",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "thinking": {"type": "enabled"},
    }
    assert "enable_thinking" not in payload


def test_transport_verifies_thinking_without_retaining_reasoning_content() -> None:
    client, requests = mock_client([success_envelope()])
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        answer, total = transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert (answer, total) == ("answer", 17)
    assert transport.last_call is not None
    assert transport.last_call.thinking_verified is True
    assert transport.last_call.reasoning_tokens == 5
    assert "private chain" not in json.dumps(dataclasses.asdict(transport.last_call))
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert requests[0].url == f"{BASE_URL}/chat/completions"
    sent_payload = json.loads(requests[0].content)
    assert sent_payload["thinking"] == {"type": "enabled"}
    assert sent_payload["max_tokens"] == 4096


def test_transport_retries_transient_failure_at_most_three_times() -> None:
    client, requests = mock_client([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), success_envelope()])
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        assert transport("secret", BASE_URL, MODEL, "s", "u")[0] == "answer"
    finally:
        client.close()

    assert len(requests) == 3
    assert transport.last_call is not None
    assert transport.last_call.attempts == 3


def test_transport_stops_after_three_transient_failures() -> None:
    client, requests = mock_client([httpx.ReadTimeout("slow") for _ in range(4)])
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        with pytest.raises(replay.RetryExhaustedTransientError) as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert len(requests) == 3
    assert transport.last_call is not None
    assert transport.last_call.attempts == 3
    assert transport.last_call.total_tokens == 0
    assert transport.last_call.thinking_verified is False
    assert transport.last_call.latency_seconds >= 0.0
    assert raised.value.metadata == transport.last_call
    assert "slow" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "actions",
    [
        [httpx.ConnectTimeout("private connect") for _ in range(3)],
        [httpx.Response(429, json={"error": "private"}) for _ in range(3)],
        [httpx.Response(503, json={"error": "private"}) for _ in range(3)],
    ],
)
def test_transport_marks_only_exhausted_retryable_failures_as_transient(
    actions: list[httpx.Response | BaseException],
) -> None:
    client, requests = mock_client(actions)
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        with pytest.raises(replay.RetryExhaustedTransientError) as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert len(requests) == 3
    assert raised.value.metadata.attempts == 3
    assert raised.value.metadata.latency_seconds >= 0.0
    assert "private" not in str(raised.value)


def test_transport_failure_drops_response_body_headers_and_authorization() -> None:
    private_body = "private provider response"
    private_header = "private-header"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            request=request,
            headers={"X-Private": private_header},
            json={"error": {"message": private_body}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=1, sleep=lambda _: None)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    error = raised.value
    assert error.response.status_code == 400
    assert error.response.content == b""
    assert private_body not in str(error)
    assert private_header not in str(error.response.headers)
    assert "secret" not in str(error.request.headers)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_transport_invalid_json_failure_does_not_retain_response_body() -> None:
    private_body = 'private response: {"reasoning_content":"private chain"'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=private_body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=1, sleep=lambda _: None)
        with pytest.raises(ValueError, match="JSON object") as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert private_body not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert transport.last_call is not None
    assert transport.last_call.attempts == 1
    assert transport.last_call.total_tokens == 0
    assert transport.last_call.thinking_verified is False


def test_transport_failure_traceback_drops_all_sensitive_request_and_response_state() -> None:
    api_key = "TRACE_API_KEY_SENTINEL"
    system_prompt = "TRACE_SYSTEM_SENTINEL"
    user_prompt = "TRACE_USER_SENTINEL"
    reasoning = "TRACE_REASONING_SENTINEL"
    raw_envelope = {
        "choices": [{"message": {"content": "answer", "reasoning_content": reasoning}}],
        "usage": {"input_tokens": "invalid"},
    }
    captured_requests: list[httpx.Request] = []
    captured_responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        response = httpx.Response(200, request=request, json=raw_envelope)
        captured_responses.append(response)
        return response

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=1, sleep=lambda _: None)
        with pytest.raises(ValueError, match="invalid token usage") as raised:
            transport(api_key, BASE_URL, MODEL, system_prompt, user_prompt)
    finally:
        client.close()

    assert_transport_exception_drops(
        raised.value,
        strings=(api_key, system_prompt, user_prompt, reasoning, "Authorization"),
        objects=(raw_envelope, captured_requests[0], captured_responses[0]),
    )


def test_failed_model_validation_clears_prior_call_metadata_and_secret_traceback() -> None:
    client, _ = mock_client([success_envelope()])
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=1, sleep=lambda _: None)
        transport("first-key", BASE_URL, MODEL, "s", "u")
        assert transport.last_call is not None

        api_key = "WRONG_MODEL_API_KEY_SENTINEL"
        with pytest.raises(ValueError, match="requires glm-5.3-flash") as raised:
            transport(api_key, BASE_URL, "wrong-model", "SYSTEM_SENTINEL", "USER_SENTINEL")
    finally:
        client.close()

    assert transport.last_call is None
    assert_transport_exception_drops(
        raised.value,
        strings=(api_key, "SYSTEM_SENTINEL", "USER_SENTINEL"),
    )


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_transport_retries_retryable_http_statuses(status_code: int) -> None:
    headers = {"Retry-After": "0", "X-Private": "private-header"}
    client, requests = mock_client(
        [
            httpx.Response(status_code, headers=headers, json={"error": "private-body"}),
            success_envelope(),
        ]
    )
    sleeps: list[float] = []
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=sleeps.append)
        assert transport("secret", BASE_URL, MODEL, "s", "u")[0] == "answer"
    finally:
        client.close()

    assert len(requests) == 2
    assert sleeps == [0.0]
    assert transport.last_call is not None
    assert transport.last_call.attempts == 2


def test_transport_clamps_provider_retry_after_to_documented_maximum() -> None:
    client, _ = mock_client(
        [
            httpx.Response(429, headers={"Retry-After": "999999"}),
            success_envelope(),
        ]
    )
    sleeps: list[float] = []
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=sleeps.append)
        transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert sleeps == [replay.GLM_MAX_RETRY_AFTER_SECONDS]


@pytest.mark.parametrize("status_code", [400, 401, 404])
def test_transport_does_not_retry_ordinary_client_errors(status_code: int) -> None:
    client, requests = mock_client(
        [
            httpx.Response(
                status_code,
                headers={"Retry-After": "7", "X-Private": "private-header"},
                json={"error": "private-body"},
            )
        ]
    )
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert len(requests) == 1
    assert raised.value.response.status_code == status_code
    assert dict(raised.value.response.headers) == {}
    assert raised.value.response.content == b""


def test_transport_does_not_retry_status_outside_http_5xx() -> None:
    client, requests = mock_client(
        [
            httpx.Response(600, json={"error": "private-body"}),
            success_envelope(),
        ]
    )
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert len(requests) == 1
    assert raised.value.response.status_code == 600


def test_transport_normalizes_input_output_token_variant_and_verifies_thinking() -> None:
    client, _ = mock_client(
        [
            {
                "choices": [{"message": {"content": "variant answer"}}],
                "usage": {
                    "input_tokens": 13,
                    "output_tokens": 9,
                    "total_tokens": 22,
                    "output_tokens_details": {"reasoning_tokens": 4},
                },
            }
        ]
    )
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=1, sleep=lambda _: None)
        result = transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert result == ("variant answer", 22)
    assert transport.last_call == replay.ReaderCallMetadata(
        input_tokens=13,
        output_tokens=9,
        reasoning_tokens=4,
        total_tokens=22,
        latency_seconds=transport.last_call.latency_seconds,
        attempts=1,
        thinking_verified=True,
    )


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
            (11, 7, 3, 18),
        ),
        (
            {
                "input_tokens": 13,
                "output_tokens": 9,
                "output_tokens_details": {"reasoning_tokens": 4},
            },
            (13, 9, 4, 22),
        ),
    ],
)
def test_response_parser_normalizes_token_field_variants(
    usage: dict[str, Any], expected: tuple[int, int, int, int]
) -> None:
    parsed = replay.parse_glm_thinking_response({"choices": [{"message": {"content": "answer"}}], "usage": usage})

    assert (
        parsed.input_tokens,
        parsed.output_tokens,
        parsed.reasoning_tokens,
        parsed.total_tokens,
    ) == expected
    assert parsed.thinking_verified is True


def test_response_without_thinking_evidence_is_unverified() -> None:
    parsed = replay.parse_glm_thinking_response(
        {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"total_tokens": 2},
        }
    )

    assert parsed.thinking_verified is False


def test_nonempty_reasoning_content_verifies_thinking_without_token_details() -> None:
    parsed = replay.parse_glm_thinking_response(
        {"choices": [{"message": {"reasoning_content": "private chain", "content": "answer"}}]}
    )

    assert parsed.thinking_verified is True
    assert not hasattr(parsed, "reasoning_content")


def test_response_parser_does_not_retain_invalid_token_value_in_exception_chain() -> None:
    private_value = "private provider token value"

    with pytest.raises(ValueError, match="invalid token usage") as raised:
        replay.parse_glm_thinking_response(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"input_tokens": private_value},
            }
        )

    assert private_value not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("usage", [None, [], True, 1, "invalid"])
def test_response_parser_rejects_present_non_mapping_usage(usage: object) -> None:
    with pytest.raises(ValueError, match="invalid token usage"):
        replay.parse_glm_thinking_response({"choices": [{"message": {"content": "answer"}}], "usage": usage})


@pytest.mark.parametrize("details", [None, [], True, 1, "invalid"])
def test_response_parser_rejects_present_non_mapping_token_details(details: object) -> None:
    with pytest.raises(ValueError, match="invalid token usage"):
        replay.parse_glm_thinking_response(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"completion_tokens_details": details},
            }
        )


@pytest.mark.parametrize("token_value", [True, False, 1.5, -1, "five"])
def test_response_parser_rejects_non_integer_or_negative_token_counts(token_value: object) -> None:
    with pytest.raises(ValueError, match="invalid token usage"):
        replay.parse_glm_thinking_response(
            {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"input_tokens": token_value},
            }
        )


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 1, "input_tokens": 1.5},
        {"completion_tokens": 1, "output_tokens": True},
        {
            "completion_tokens_details": {"reasoning_tokens": 1},
            "output_tokens_details": [],
        },
        {
            "completion_tokens_details": {"reasoning_tokens": 1},
            "output_tokens_details": {"reasoning_tokens": 1.5},
        },
    ],
)
def test_response_parser_rejects_malformed_secondary_usage_variants(
    usage: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="invalid token usage"):
        replay.parse_glm_thinking_response({"choices": [{"message": {"content": "answer"}}], "usage": usage})


def test_response_parser_reads_secondary_reasoning_details_when_primary_omits_count() -> None:
    parsed = replay.parse_glm_thinking_response(
        {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {
                "completion_tokens_details": {},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        }
    )

    assert parsed.reasoning_tokens == 3
    assert parsed.thinking_verified is True


@pytest.mark.parametrize("content", [None, "", "   "])
def test_response_parser_rejects_missing_or_empty_final_content(content: object) -> None:
    envelope = {"choices": [{"message": {"content": content}}]}

    with pytest.raises(ValueError, match="final answer"):
        replay.parse_glm_thinking_response(envelope)


def synthetic_three_arm_cases(
    *,
    include_canary: bool,
) -> dict[str, tuple[replay.ReplayCase, ...]]:
    case_ids = [f"memdaily:simple:events:{index}" for index in range(1, 41)]
    if include_canary:
        case_ids[13] = replay.CANARY_CASE_ID
    result: dict[str, tuple[replay.ReplayCase, ...]] = {}
    for arm_number, label in enumerate(replay.ARM_LABELS, start=1):
        arm = replay.SourceArm(
            label=label,
            report_path=Path(f"synthetic-{label}.json"),
            report_sha256=str(arm_number) * 64,
            extractor_model=replay.EXPECTED_EXTRACTOR_MODELS[label],
            manifest_sha256="a" * 64,
            scoring_inputs_sha256="0" * 64,
        )
        cases: list[replay.ReplayCase] = []
        for index, case_id in enumerate(case_ids):
            dataset = "perltqa" if case_id == replay.CANARY_CASE_ID else "memdaily"
            trajectory = replace(
                make_trajectory(case_id=case_id),
                answer="synthetic answer",
                messages=(),
            )
            source_case = make_case(case_id)
            source_case.update(
                dataset=dataset,
                slice="perltqa_dialogues" if dataset == "perltqa" else "memdaily_simple",
                answer="synthetic answer",
                retrieval={"recall_at_5": 1.0, "mrr": 1.0},
                ingest={"stored_claims": 1},
                gold_extraction_units=[f"event-{case_id}"],
                covered_extraction_units=[f"event-{case_id}"],
            )
            source_case["qa"].update(
                gold_answer="synthetic answer",
                predicted_answer="synthetic answer" if index % 2 == 0 else "wrong",
                exact_match=index % 2 == 0,
                f1=1.0 if index % 2 == 0 else 0.0,
                answer_correct=float(index % 2 == 0),
            )
            retrieved = (
                {"rank": 1, "text": f"first evidence {case_id}", "entities": [], "seed_rank": 1},
                {"rank": 2, "text": f"second evidence {case_id}", "entities": [], "seed_rank": 2},
            )
            source_case["retrieved"] = [dict(item) for item in retrieved]
            cases.append(
                replay.ReplayCase(
                    arm=arm,
                    case_id=case_id,
                    dataset=dataset,
                    slice_name=str(source_case["slice"]),
                    trajectory=trajectory,
                    answer_anchors=("synthetic answer",) if dataset == "perltqa" else (),
                    accepted_rubrics=(),
                    answer_entity_gold=make_gold(case_id),
                    retrieved=retrieved,
                    source_case=source_case,
                )
            )
        scoring_inputs_sha256 = replay._scoring_inputs_sha256(cases)
        bound_arm = replace(arm, scoring_inputs_sha256=scoring_inputs_sha256)
        result[label] = tuple(replace(case, arm=bound_arm) for case in cases)
    return result


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (True, True, "unchanged_correct"),
        (False, False, "unchanged_wrong"),
        (False, True, "wrong_to_right"),
        (True, False, "right_to_wrong"),
    ],
)
def test_classify_flip(before: bool, after: bool, expected: str) -> None:
    assert replay.classify_flip(before, after) == expected


def test_ranking_uses_accuracy_only_and_groups_ties_deterministically() -> None:
    arms = {
        "qwen37": {
            "original": {"qa_accuracy": 0.85, "qa_f1": 0.1},
            "replay": {"qa_accuracy": 0.9, "qa_f1": 0.1},
        },
        "glm53": {
            "original": {"qa_accuracy": 0.825, "qa_f1": 0.9},
            "replay": {"qa_accuracy": 0.825, "qa_f1": 0.9},
        },
        "qwen38-27b": {
            "original": {"qa_accuracy": 0.8, "qa_f1": 1.0},
            "replay": {"qa_accuracy": 0.825, "qa_f1": 0.1},
        },
    }

    assert replay._ranking(arms, "original") == [["qwen37"], ["glm53"], ["qwen38-27b"]]
    assert replay._ranking(arms, "replay") == [["qwen37"], ["glm53", "qwen38-27b"]]


def test_incomplete_summary_omits_rankings(tmp_path: Path) -> None:
    summary = replay.run_replay(
        synthetic_three_arm_cases(include_canary=True),
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )

    assert summary["status"] == "canary_completed"
    assert "original_ranking" not in summary
    assert "replay_ranking" not in summary


def test_reader_identity_is_complete_and_persists_only_an_endpoint_digest() -> None:
    identity = replay._reader_identity(BASE_URL)

    assert identity == {
        "model": "glm-5.3-flash",
        "temperature": 0.1,
        "max_tokens": 4096,
        "timeout_seconds": 120.0,
        "endpoint_sha256": "0413b53d28826c51b400bc9ebc578639bf6a4ff94d3e43fbcbc468bd51945602",
    }
    assert BASE_URL not in json.dumps(identity)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example/v1",
        "https://user:password@provider.example/v1",
        "https://provider.example/v1?token=private",
        "https://provider.example/v1#private",
        "https:///missing-host",
    ],
)
def test_invalid_endpoint_is_rejected_before_transport_call(
    tmp_path: Path,
    base_url: str,
) -> None:
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayAbortedError, match="reader replay aborted"):
        replay.run_replay(
            synthetic_three_arm_cases(include_canary=True),
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=False,
            base_url=base_url,
        )

    assert transport.calls == []
    state = json.loads((tmp_path / replay.AUTHORITATIVE_STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "aborted"
    assert state["abort_stage"] == "input_validation"
    assert state["case_states"] == {label: [] for label in replay.ARM_LABELS}
    assert base_url not in json.dumps(state)


def test_malformed_source_mapping_persists_sanitized_preflight_abort(tmp_path: Path) -> None:
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayAbortedError, match="reader replay aborted") as raised:
        replay.run_replay(
            {},
            transport,
            output_root=tmp_path,
            canary_only=False,
            resume=False,
        )

    assert transport.calls == []
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    state = json.loads((tmp_path / replay.AUTHORITATIVE_STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "aborted"
    assert state["abort_stage"] == "input_validation"
    assert state["identity_complete"] is False


def test_transport_invalid_endpoint_exception_drops_embedded_credentials() -> None:
    client, _ = mock_client([success_envelope()])
    transport = replay.GLMThinkingTransport(client, max_attempts=1, sleep=lambda _: None)
    bad_url = "https://PRIVATE_USER:PRIVATE_PASSWORD@provider.example/v1?token=PRIVATE_TOKEN"
    try:
        with pytest.raises(replay.ReplayInputError, match="HTTPS endpoint") as raised:
            transport("PRIVATE_API_KEY", bad_url, MODEL, "PRIVATE_SYSTEM", "PRIVATE_USER_PROMPT")
    finally:
        client.close()

    assert_transport_exception_drops(
        raised.value,
        strings=(
            "PRIVATE_USER",
            "PRIVATE_PASSWORD",
            "PRIVATE_TOKEN",
            "PRIVATE_API_KEY",
            "PRIVATE_SYSTEM",
            "PRIVATE_USER_PROMPT",
        ),
    )


def test_checkpoint_and_summary_bind_complete_input_identity(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    summary = replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    checkpoint = json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))

    for artifact in (checkpoint, summary):
        assert artifact["manifest_sha256"] == "a" * 64
        assert artifact["scoring_inputs_sha256"] == sources["qwen37"][0].arm.scoring_inputs_sha256
        assert artifact["extractor_models"] == replay.EXPECTED_EXTRACTOR_MODELS


def test_summary_contains_explicit_safe_canary_evidence(tmp_path: Path) -> None:
    summary = replay.run_replay(
        synthetic_three_arm_cases(include_canary=True),
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )

    assert summary["canary"] == {
        "arm": "qwen37",
        "case_id": replay.CANARY_CASE_ID,
        "thinking_verified": True,
        "attempts": 2,
        "input_tokens": 10,
        "output_tokens": 7,
        "reasoning_tokens": 5,
        "total_tokens": 17,
        "latency_seconds": 0.25,
    }


@pytest.mark.parametrize("mutation", ["manifest", "question", "rubrics", "gold"])
def test_resume_rejects_reconstructed_input_mutation_before_call(
    tmp_path: Path,
    mutation: str,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    for label in replay.ARM_LABELS:
        cases = list(sources[label])
        first = cases[0]
        if mutation == "manifest":
            cases = [replace(case, arm=replace(case.arm, manifest_sha256="b" * 64)) for case in cases]
        elif mutation == "question":
            cases[0] = replace(first, trajectory=replace(first.trajectory, question="mutated question"))
        elif mutation == "rubrics":
            cases[0] = replace(first, accepted_rubrics=((("mutated rubric",),),))
        else:
            cases[0] = replace(first, answer_entity_gold=make_gold("mutated-gold"))
        sources[label] = tuple(cases)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="manifest|scoring inputs|source identity"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=True,
        )

    assert transport.calls == []


def test_score_replayed_case_reuses_prompt_evidence_and_answerability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HL_MEM_EVAL_QA_API_KEY", raising=False)
    monkeypatch.delenv("HL_MEM_EVAL_QA_BASE_URL", raising=False)
    monkeypatch.delenv("HL_MEM_EVAL_QA_MODEL", raising=False)
    replay_case = synthetic_three_arm_cases(include_canary=True)["qwen37"][13]
    original = deepcopy(replay_case.source_case)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    scored = replay.score_replayed_case(
        replay_case,
        transport,
        api_key="synthetic-secret",
        base_url=BASE_URL,
    )

    assert len(transport.calls) == 1
    api_key, base_url, model, _, user_prompt = transport.calls[0]
    assert (api_key, base_url, model) == ("synthetic-secret", BASE_URL, MODEL)
    assert user_prompt.index("first evidence") < user_prompt.index("second evidence")
    assert scored["qa"]["answerability"] == original["qa"]["answerability"]
    assert scored["qa"]["answer_correct"] == 1.0
    assert scored["retrieved"] == original["retrieved"]
    assert scored["retrieval"] == original["retrieval"]
    assert scored["ingest"] == original["ingest"]
    assert scored["reader_call"]["thinking_verified"] is True
    assert replay_case.source_case == original
    assert "HL_MEM_EVAL_QA_API_KEY" not in os.environ
    assert "HL_MEM_EVAL_QA_BASE_URL" not in os.environ
    assert "HL_MEM_EVAL_QA_MODEL" not in os.environ


def test_run_replay_calls_canary_first_and_counts_it_once(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)
    summary = replay.run_replay(sources, transport, output_root=tmp_path, canary_only=False, resume=False)

    assert summary["execution_order"][0] == f"qwen37:{replay.CANARY_CASE_ID}"
    assert len(transport.calls) == 120
    assert sum(item.endswith(replay.CANARY_CASE_ID) for item in summary["execution_order"]) == 3
    assert summary["status"] == "completed"
    assert summary["logical_calls"] == 120
    assert set(summary["arms"]) == set(replay.ARM_LABELS)
    assert all(len(summary["arms"][label]["flips"]["wrong_to_right"]) == 20 for label in replay.ARM_LABELS)
    assert "gate" not in summary


def test_canary_only_checkpoints_one_call_and_resume_does_not_repeat_it(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    first = FakeThinkingTransport(answer="synthetic answer", verified=True)
    partial = replay.run_replay(sources, first, output_root=tmp_path, canary_only=True, resume=False)
    assert partial["status"] == "canary_completed"
    assert len(first.calls) == 1
    assert json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["completed_case_ids"] == [
        replay.CANARY_CASE_ID
    ]
    assert json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["completed_at"] is not None
    assert json.loads((tmp_path / "glm53.json").read_text(encoding="utf-8"))["completed_at"] is None

    second = FakeThinkingTransport(answer="synthetic answer", verified=True)
    complete = replay.run_replay(sources, second, output_root=tmp_path, canary_only=False, resume=True)
    assert len(second.calls) == 119
    assert complete["logical_calls"] == 120


def test_authoritative_state_commits_first_and_repairs_projections_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    real_write = replay._write_json_atomic
    writes: list[str] = []

    def interrupt_after_state(path: Path, payload: dict[str, Any]) -> None:
        writes.append(path.name)
        if path.name == "qwen37.json":
            raise OSError("synthetic projection interruption")
        real_write(path, payload)

    monkeypatch.setattr(replay, "_write_json_atomic", interrupt_after_state)
    with pytest.raises(OSError, match="projection interruption"):
        replay.run_replay(
            sources,
            FakeThinkingTransport(answer="synthetic answer", verified=True),
            output_root=tmp_path,
            canary_only=True,
            resume=False,
        )

    assert writes[:2] == ["state.json", "qwen37.json"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "canary_completed"
    assert state["logical_calls"] == 1

    monkeypatch.setattr(replay, "_write_json_atomic", real_write)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)
    repaired = replay.run_replay(
        sources,
        transport,
        output_root=tmp_path,
        canary_only=True,
        resume=True,
    )

    assert transport.calls == []
    assert repaired["status"] == "canary_completed"
    assert json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["completed_case_ids"] == [
        replay.CANARY_CASE_ID
    ]
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["logical_calls"] == 1


def convert_completed_replay_to_legacy(output_root: Path) -> None:
    for label in replay.ARM_LABELS:
        path = output_root / f"{label}.json"
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        checkpoint["schema_version"] = replay.LEGACY_REPLAY_SCHEMA_VERSION
        checkpoint["reader"] = {"model": MODEL, "base_url": BASE_URL}
        checkpoint["source"].pop("extractor_model")
        for field in ("manifest_sha256", "scoring_inputs_sha256", "extractor_models"):
            checkpoint.pop(field)
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
    summary_path = output_root / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["schema_version"] = replay.LEGACY_REPLAY_SCHEMA_VERSION
    summary["reader"] = {"model": MODEL, "base_url": BASE_URL}
    for field in ("manifest_sha256", "scoring_inputs_sha256", "extractor_models"):
        summary.pop(field)
    summary_path.write_text(json.dumps(summary), encoding="utf-8")


def test_completed_legacy_replay_migrates_with_backup_and_zero_calls(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    (tmp_path / replay.AUTHORITATIVE_STATE_FILE).unlink()
    convert_completed_replay_to_legacy(tmp_path)
    artifact_names = [f"{label}.json" for label in replay.ARM_LABELS] + ["summary.json"]
    before = {name: replay.sha256_file(tmp_path / name) for name in artifact_names}
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    summary = replay.run_replay(
        sources,
        transport,
        output_root=tmp_path,
        canary_only=False,
        resume=True,
    )

    assert transport.calls == []
    assert summary["status"] == "completed"
    state = json.loads((tmp_path / replay.AUTHORITATIVE_STATE_FILE).read_text(encoding="utf-8"))
    assert state["logical_calls"] == 120
    assert state["migration"]["from_schema_version"] == replay.LEGACY_REPLAY_SCHEMA_VERSION
    backup = tmp_path / replay.LEGACY_BACKUP_DIRECTORY
    assert {name: replay.sha256_file(backup / name) for name in artifact_names} == before


def test_partial_legacy_replay_is_rejected_without_rewrite_or_transport_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    (tmp_path / replay.AUTHORITATIVE_STATE_FILE).unlink()
    convert_completed_replay_to_legacy(tmp_path)
    mutate_json(tmp_path / "summary.json", lambda value: value.update(status="running"))
    before = replay.sha256_file(tmp_path / "summary.json")
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="complete.*legacy"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=False,
            resume=True,
        )

    assert transport.calls == []
    assert replay.sha256_file(tmp_path / "summary.json") == before
    assert not (tmp_path / replay.AUTHORITATIVE_STATE_FILE).exists()
    assert not (tmp_path / replay.LEGACY_BACKUP_DIRECTORY).exists()


def test_unverified_canary_aborts_before_second_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=False)
    summary = replay.run_replay(sources, transport, output_root=tmp_path, canary_only=False, resume=False)
    assert summary["status"] == "mode_unverified"
    assert len(transport.calls) == 1
    assert json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["status"] == "mode_unverified"


def test_checkpoint_contains_no_secret_envelope_or_reasoning(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)
    replay.run_replay(
        sources,
        transport,
        output_root=tmp_path,
        canary_only=True,
        resume=False,
        api_key="Bearer SYNTHETIC_SECRET",
    )
    raw = (tmp_path / "qwen37.json").read_text(encoding="utf-8")
    assert "private chain" not in raw
    assert "Bearer " not in raw
    assert '"reasoning_content"' not in raw


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["sources"]["qwen37"].update(sha256="0" * 64), "sources"),
        (lambda value: value["reader"].update(model="other"), "reader"),
        (lambda value: value.update(thinking={"type": "disabled"}), "thinking"),
        (lambda value: value["versions"].update(prompt="other"), "versions"),
        (lambda value: value["case_sets"]["qwen37"].pop(), "case_sets"),
    ],
)
def test_resume_rejects_authoritative_identity_mismatch_before_call(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    path = tmp_path / replay.AUTHORITATIVE_STATE_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match=message):
        replay.run_replay(sources, transport, output_root=tmp_path, canary_only=False, resume=True)

    assert transport.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda case: case["qa"].update(exact_match=False, answer_correct=False),
        lambda case: case["answer_entity"].update(entity_coverage_at_5=0.75),
        lambda case: case["reader_call"].update(attempts=99),
        lambda case: case["qa"]["usage"].update(private_extra="not-safe"),
        lambda case: case.update(messages=[{"content": "PRIVATE_MESSAGE"}]),
        lambda case: case.update(question="tampered question"),
    ],
)
def test_resume_rejects_tampered_non_canary_authoritative_result(
    tmp_path: Path,
    mutation: Any,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    path = tmp_path / replay.AUTHORITATIVE_STATE_FILE
    state = json.loads(path.read_text(encoding="utf-8"))
    mutation(state["case_states"]["qwen37"][1])
    path.write_text(json.dumps(state), encoding="utf-8")
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="authoritative replay case result"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=False,
            resume=True,
        )

    assert transport.calls == []


def test_resume_repairs_corrupt_projection_before_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    (tmp_path / "glm53.json").write_text("{broken", encoding="utf-8")
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    summary = replay.run_replay(sources, transport, output_root=tmp_path, canary_only=True, resume=True)

    assert transport.calls == []
    assert summary["status"] == "canary_completed"
    assert json.loads((tmp_path / "glm53.json").read_text(encoding="utf-8"))["status"] == "pending"


def test_resume_repairs_tampered_projection_metrics_before_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    path = tmp_path / "qwen37.json"
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    checkpoint["metrics"]["overall"]["qa_accuracy"] = 0.0
    path.write_text(json.dumps(checkpoint), encoding="utf-8")
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    replay.run_replay(sources, transport, output_root=tmp_path, canary_only=True, resume=True)

    assert transport.calls == []
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["metrics"]["overall"]["qa_accuracy"] != 0.0


def test_partial_failures_are_checkpointed_and_do_not_stop_later_cases(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True, fail_on_calls={2, 42})

    summary = replay.run_replay(sources, transport, output_root=tmp_path, canary_only=False, resume=False)

    assert len(transport.calls) == 120
    assert summary["status"] == "completed_with_failures"
    assert summary["logical_calls"] == 120
    assert len(summary["failed_case_ids"]) == 2
    assert "original_ranking" not in summary
    assert "replay_ranking" not in summary
    assert sum(len(arm["failed_case_ids"]) for arm in summary["arms"].values()) == 2
    assert summary["arms"]["qwen37"]["paired_delta"]["qa_accuracy"] == pytest.approx(20 / 39)


def test_scorer_failure_persists_aborted_state_and_raises_generic_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)

    def fail_score(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        raise RuntimeError("PRIVATE_SCORER_FAILURE")

    monkeypatch.setattr(replay, "score_answer", fail_score)

    with pytest.raises(replay.ReplayAbortedError, match="reader replay aborted") as raised:
        replay.run_replay(
            sources,
            FakeThinkingTransport(answer="synthetic answer", verified=True),
            output_root=tmp_path,
            canary_only=False,
            resume=False,
        )

    summary_raw = (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert json.loads(summary_raw)["status"] == "aborted"
    assert "PRIVATE_SCORER_FAILURE" not in summary_raw
    assert "PRIVATE_SCORER_FAILURE" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_main_rejects_missing_or_placeholder_secret_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(replay, "load_replay_cases", lambda manifest, sources: pytest.fail("loaded sources"))
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=xxx\n", encoding="utf-8")

    exit_code = replay.main(["--env-file", str(env_file), "--output-root", str(tmp_path / "out")])

    assert exit_code != 0


def test_main_returns_nonzero_when_canary_cannot_verify_thinking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", lambda manifest, paths: sources)

    class FakeClient:
        def __init__(self, *, timeout: float = 5.0) -> None:
            del timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(replay.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        replay,
        "GLMThinkingTransport",
        lambda client: FakeThinkingTransport(answer="synthetic answer", verified=False),
    )

    exit_code = replay.main(["--output-root", str(tmp_path / "out")])

    assert exit_code != 0


def test_main_persists_generic_abort_for_fresh_source_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    sources["qwen37"] = tuple(
        replace(
            case,
            arm=replace(case.arm, extractor_model="PRIVATE_MISLABELED_EXTRACTOR"),
        )
        for case in sources["qwen37"]
    )
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    class FakeClient:
        def __init__(self, *, timeout: object = None) -> None:
            del timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", lambda manifest, paths: sources)
    monkeypatch.setattr(replay.httpx, "Client", FakeClient)
    monkeypatch.setattr(replay, "GLMThinkingTransport", lambda client: transport)
    output_root = tmp_path / "out"

    exit_code = replay.main(["--output-root", str(output_root)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert transport.calls == []
    assert captured.err.strip() == "reader replay failed: reader replay aborted"
    state_raw = (output_root / replay.AUTHORITATIVE_STATE_FILE).read_text(encoding="utf-8")
    assert json.loads(state_raw)["status"] == "aborted"
    assert "PRIVATE_MISLABELED_EXTRACTOR" not in captured.err


def test_main_persists_generic_preflight_abort_when_source_loading_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_load(manifest: Path, paths: dict[str, Path]) -> dict[str, tuple[replay.ReplayCase, ...]]:
        del manifest, paths
        raise replay.ReplayInputError("PRIVATE_MANIFEST_OR_REPORT_FAILURE")

    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", fail_load)
    output_root = tmp_path / "out"

    exit_code = replay.main(["--output-root", str(output_root)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.err.strip() == "reader replay failed: reader replay aborted"
    state_raw = (output_root / replay.AUTHORITATIVE_STATE_FILE).read_text(encoding="utf-8")
    state = json.loads(state_raw)
    assert state["status"] == "aborted"
    assert state["abort_stage"] == "input_validation"
    assert "PRIVATE_MANIFEST_OR_REPORT_FAILURE" not in state_raw


def test_main_resume_preflight_failure_preserves_existing_authoritative_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = tmp_path / "out"
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=output_root,
        canary_only=True,
        resume=False,
    )
    state_path = output_root / replay.AUTHORITATIVE_STATE_FILE
    before = replay.sha256_file(state_path)

    def fail_load(manifest: Path, paths: dict[str, Path]) -> dict[str, tuple[replay.ReplayCase, ...]]:
        del manifest, paths
        raise replay.ReplayInputError("PRIVATE_RESUME_PREFLIGHT_FAILURE")

    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", fail_load)

    exit_code = replay.main(["--resume", "--output-root", str(output_root)])

    assert exit_code == 2
    assert capsys.readouterr().err.strip() == "reader replay failed: reader replay aborted"
    assert replay.sha256_file(state_path) == before


def test_main_resume_preflight_failure_preserves_unmigrated_legacy_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    state_path = tmp_path / replay.AUTHORITATIVE_STATE_FILE
    state_path.unlink()
    convert_completed_replay_to_legacy(tmp_path)
    legacy_paths = [tmp_path / f"{label}.json" for label in replay.ARM_LABELS] + [tmp_path / "summary.json"]
    before = {path.name: replay.sha256_file(path) for path in legacy_paths}

    def fail_load(manifest: Path, paths: dict[str, Path]) -> dict[str, tuple[replay.ReplayCase, ...]]:
        del manifest, paths
        raise replay.ReplayInputError("PRIVATE_LEGACY_RESUME_PREFLIGHT_FAILURE")

    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", fail_load)

    assert replay.main(["--resume", "--output-root", str(tmp_path)]) == 2
    assert not state_path.exists()
    assert {path.name: replay.sha256_file(path) for path in legacy_paths} == before


def test_main_classifies_exhausted_transient_canary_as_continuable_reader_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(
        answer="synthetic answer",
        verified=True,
        fail_on_calls={1},
    )

    class FakeClient:
        def __init__(self, *, timeout: object = None) -> None:
            del timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", lambda manifest, paths: sources)
    monkeypatch.setattr(replay.httpx, "Client", FakeClient)
    monkeypatch.setattr(replay, "GLMThinkingTransport", lambda client: transport)
    output_root = tmp_path / "out"

    exit_code = replay.main(["--output-root", str(output_root)])

    assert exit_code == 1
    state = json.loads((output_root / replay.AUTHORITATIVE_STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "canary_failed"
    assert state["case_states"]["qwen37"][0]["reader_error"] == "reader_call_failed"
    assert state["case_states"]["qwen37"][0]["reader_call"]["attempts"] == 3


def test_main_constructs_http_client_with_a_120_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    constructed_timeouts: list[object] = []

    class RecordingClient:
        def __init__(self, *, timeout: object = None) -> None:
            constructed_timeouts.append(timeout)

        def __enter__(self) -> RecordingClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setenv("LLM_API_KEY", "synthetic-secret")
    monkeypatch.setattr(replay, "load_replay_cases", lambda manifest, paths: {})
    monkeypatch.setattr(replay.httpx, "Client", RecordingClient)
    monkeypatch.setattr(replay, "run_replay", lambda *args, **kwargs: {"status": "canary_completed"})

    assert replay.main(["--output-root", str(tmp_path / "out")]) == 0
    assert constructed_timeouts == [120.0]


def mutate_json(path: Path, mutation: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_resume_rejects_unknown_summary_status_before_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    mutate_json(tmp_path / replay.AUTHORITATIVE_STATE_FILE, lambda value: value.update(status="invented"))
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="status"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=True,
        )

    assert transport.calls == []


def test_resume_repairs_projection_status_from_authoritative_state(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    mutate_json(tmp_path / "qwen37.json", lambda value: value.update(status="pending"))
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    replay.run_replay(
        sources,
        transport,
        output_root=tmp_path,
        canary_only=True,
        resume=True,
    )

    assert transport.calls == []
    assert json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["status"] == "canary_completed"


def test_resume_rejects_canary_with_tampered_thinking_verification(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )

    def remove_verification(value: dict[str, Any]) -> None:
        value["case_states"]["qwen37"][0]["reader_call"]["thinking_verified"] = False

    mutate_json(tmp_path / replay.AUTHORITATIVE_STATE_FILE, remove_verification)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="canary"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=True,
        )

    assert transport.calls == []


def test_resume_rejects_reordered_physical_execution_prefix(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )

    def reorder(value: dict[str, Any]) -> None:
        value["execution_order"][0], value["execution_order"][1] = (
            value["execution_order"][1],
            value["execution_order"][0],
        )

    mutate_json(tmp_path / replay.AUTHORITATIVE_STATE_FILE, reorder)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="execution order"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=False,
            resume=True,
        )

    assert transport.calls == []


def test_resume_rejects_arbitrary_expected_case_substituted_for_canary(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True),
        output_root=tmp_path,
        canary_only=True,
        resume=False,
    )
    replacement = sources["qwen37"][0].case_id

    def substitute_state(value: dict[str, Any]) -> None:
        value["case_states"]["qwen37"][0]["case_id"] = replacement
        value["execution_order"] = [f"qwen37:{replacement}"]

    mutate_json(tmp_path / replay.AUTHORITATIVE_STATE_FILE, substitute_state)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="execution order"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=True,
        )

    assert transport.calls == []


def test_resume_rejects_unverified_canary_with_status_tampered_to_completed(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=False),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    mutate_json(
        tmp_path / replay.AUTHORITATIVE_STATE_FILE,
        lambda value: value.update(status="canary_completed"),
    )
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="canary"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=True,
        )

    assert transport.calls == []


def test_resume_rejects_failed_canary_with_status_tampered_to_completed(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True, fail_on_calls={1}),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    mutate_json(
        tmp_path / replay.AUTHORITATIVE_STATE_FILE,
        lambda value: value.update(status="canary_completed"),
    )
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    with pytest.raises(replay.ReplayInputError, match="canary"):
        replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=True,
            resume=True,
        )

    assert transport.calls == []


def test_retry_exhaustion_persists_terminal_attempt_and_latency_totals(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    client, requests = mock_client([httpx.ReadTimeout("private timeout") for _ in range(3)])
    try:
        transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
        summary = replay.run_replay(
            sources,
            transport,
            output_root=tmp_path,
            canary_only=False,
            resume=False,
        )
    finally:
        client.close()

    assert len(requests) == 3
    assert summary["status"] == "canary_failed"
    assert summary["arms"]["qwen37"]["reader_totals"]["attempts"] == 3
    assert summary["arms"]["qwen37"]["reader_totals"]["latency_seconds"] >= 0.0
    [failed] = json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["cases"]
    assert failed["reader_call"]["attempts"] == 3
    assert failed["reader_call"]["total_tokens"] == 0
    assert failed["reader_call"]["thinking_verified"] is False
    assert "private timeout" not in json.dumps(failed)


def test_orchestration_does_not_persist_stale_metadata_from_a_prior_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)

    class StaleTransport:
        last_call: replay.ReaderCallMetadata | None = replay.ReaderCallMetadata(
            9,
            9,
            9,
            27,
            9.0,
            9,
            True,
        )

        def __call__(self, *args: str) -> tuple[str, int]:
            raise RuntimeError("failed without clearing metadata")

    with pytest.raises(replay.ReplayAbortedError, match="reader replay aborted") as raised:
        replay.run_replay(
            sources,
            StaleTransport(),
            output_root=tmp_path,
            canary_only=False,
            resume=False,
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "aborted"
    assert summary["logical_calls"] == 0
    assert summary["aborted_case_id"] == f"qwen37:{replay.CANARY_CASE_ID}"
    assert "original_ranking" not in summary
    assert "replay_ranking" not in summary


def test_reader_failure_preserves_non_reader_metrics_and_source_error(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    sources["qwen37"][0].source_case["retrieval"] = {"recall_at_5": 0.25, "mrr": 0.125}
    original_metrics = replay.aggregate_results([case.source_case for case in sources["qwen37"]])["overall"]

    replay.run_replay(
        sources,
        FakeThinkingTransport(answer="synthetic answer", verified=True, fail_on_calls={2}),
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )

    checkpoint = json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))
    failed = checkpoint["cases"][1]
    assert failed["error"] is None
    assert failed["reader_error"] == "reader_call_failed"
    assert failed["qa"] is None
    assert failed["answer_entity"] is None
    for metric in ("recall_at_5", "mrr", "extraction_coverage"):
        assert checkpoint["metrics"]["overall"][metric] == original_metrics[metric]


def test_resume_does_not_retry_completed_reader_failures(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    first = FakeThinkingTransport(answer="synthetic answer", verified=True, fail_on_calls={2, 42})
    partial = replay.run_replay(
        sources,
        first,
        output_root=tmp_path,
        canary_only=False,
        resume=False,
    )
    expected_qwen_ids = [replay.CANARY_CASE_ID] + [
        case.case_id for case in sources["qwen37"] if case.case_id != replay.CANARY_CASE_ID
    ]
    assert json.loads((tmp_path / "qwen37.json").read_text(encoding="utf-8"))["completed_case_ids"] == (
        expected_qwen_ids
    )
    for label in replay.ARM_LABELS[1:]:
        assert json.loads((tmp_path / f"{label}.json").read_text(encoding="utf-8"))["completed_case_ids"] == [
            case.case_id for case in sources[label]
        ]
    resumed_transport = FakeThinkingTransport(answer="synthetic answer", verified=True)

    resumed = replay.run_replay(
        sources,
        resumed_transport,
        output_root=tmp_path,
        canary_only=False,
        resume=True,
    )

    assert resumed_transport.calls == []
    assert resumed["execution_order"] == partial["execution_order"]
    assert resumed["failed_case_ids"] == partial["failed_case_ids"]
