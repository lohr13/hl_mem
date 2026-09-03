from __future__ import annotations

import dataclasses
import json
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
    traceback = error.__traceback__
    while traceback is not None:
        module_name = str(traceback.tb_frame.f_globals.get("__name__", ""))
        if module_name in {
            replay.__name__,
            "evaluation.tools.longmemeval.qa_client",
            "hl_mem.http_utils",
        }:
            roots.extend(traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next

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
        with pytest.raises(httpx.ReadTimeout, match="timed out") as raised:
            transport("secret", BASE_URL, MODEL, "s", "u")
    finally:
        client.close()

    assert len(requests) == 3
    assert transport.last_call is None
    assert "slow" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_transport_failure_drops_response_body_headers_and_authorization() -> None:
    private_body = "private provider response"
    private_header = "private-header"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
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
    assert error.response.status_code == 500
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
    assert transport.last_call is None


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
    assert raised.value.response.headers["Retry-After"] == "7"
    assert "X-Private" not in raised.value.response.headers
    assert raised.value.response.content == b""


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
