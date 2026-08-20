from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evaluation.v0291_behavioral.agent import ModelCallResult
from evaluation.v0291_behavioral.scorer import (
    JUDGE_SCHEMA,
    MODEL_SNAPSHOT,
    BehavioralScorer,
    CompatibleStructuredTransport,
    JudgmentInvalid,
    build_judge_input,
    load_cwd_api_key,
    load_sentinels,
    sentinel_mismatches,
    validate_judgment,
)

ROOT = Path(__file__).resolve().parents[2]
SENTINEL_PATH = ROOT / "tests/fixtures/v0291_judge_sentinels.json"


class FakeTransport:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> ModelCallResult:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        return ModelCallResult(
            output=output,
            request_id=f"judge-{len(self.calls)}",
            input_tokens=50,
            output_tokens=10,
        )


def _sentinel(index: int = 0) -> dict[str, Any]:
    return load_sentinels(SENTINEL_PATH)[index]


def test_sentinel_fixture_covers_all_nine_frozen_judgment_branches() -> None:
    sentinels = load_sentinels(SENTINEL_PATH)

    assert len(sentinels) == 9
    assert len({item["opaque_sample_id"] for item in sentinels}) == 9
    assert {item["sentinel_kind"] for item in sentinels} == {
        "executed_specific_before_action",
        "generic_after_action",
        "missing_trace",
        "proposed_specific_before_action",
        "stable_accepted",
        "stable_caveated",
        "stable_overwritten",
        "stable_rejected_for_age",
        "unsafe_unverified_reliance",
    }
    assert all(item["expected_judgment"] for item in sentinels)


def test_judge_input_exposes_only_frozen_applicability_metadata() -> None:
    sentinel = _sentinel()
    payload = build_judge_input(sentinel, sentinel["trace"])
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert payload["applicable_dimensions"] == [
        "obsolete_acceptance",
        "verification_action",
        "final_attribution",
        "unsupported_new_configuration",
    ]
    assert sentinel["sentinel_kind"] not in serialized
    assert "expected_judgment" not in serialized
    assert "cohort" not in serialized
    assert "arm_name" not in serialized


def test_sentinel_traces_support_every_definitive_expected_dimension() -> None:
    sentinels = load_sentinels(SENTINEL_PATH)

    for sentinel in sentinels:
        validate_judgment(sentinel["expected_judgment"], sentinel["trace"], sentinel)
        expected = sentinel["expected_judgment"]
        if expected["final_attribution"] != "incorrect":
            continue
        trace_text = json.dumps(sentinel["trace"], ensure_ascii=False)
        assert "8181" in trace_text, sentinel["sentinel_kind"]
        assert "8080" in trace_text, sentinel["sentinel_kind"]
        assert "旧 SOP" in trace_text, sentinel["sentinel_kind"]


def test_missing_trace_sentinel_uses_an_empty_ordered_trace() -> None:
    sentinel = next(item for item in load_sentinels(SENTINEL_PATH) if item["sentinel_kind"] == "missing_trace")

    assert sentinel["trace"] == []


def test_stable_caveat_sentinel_makes_the_noninvalidating_caveat_explicit() -> None:
    sentinel = next(item for item in load_sentinels(SENTINEL_PATH) if item["sentinel_kind"] == "stable_caveated")

    trace_text = json.dumps(sentinel["trace"], ensure_ascii=False)
    assert "不否定其有效性" in trace_text


def test_api_key_is_read_only_from_cwd_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "LLM_API_KEY='cwd-secret'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "process-secret")

    assert load_cwd_api_key() == "cwd-secret"


def test_sentinel_comparison_uses_frozen_enums_not_evidence_wording() -> None:
    sentinel = _sentinel()
    record = {"call_status": "ok", "judge_output": sentinel["expected_judgment"]}

    assert sentinel_mismatches(record, sentinel) == {}
    changed = json.loads(json.dumps(record))
    changed["judge_output"]["verification_action"] = "generic_or_after_action"
    assert sentinel_mismatches(changed, sentinel) == {
        "verification_action": {
            "expected": "none",
            "actual": "generic_or_after_action",
        }
    }


def test_judgment_validator_rejects_wrong_id_quote_and_applicability() -> None:
    sentinel = _sentinel()
    valid = sentinel["expected_judgment"]
    validate_judgment(valid, sentinel["trace"], sentinel)

    wrong_id = dict(valid, sample_id="0" * 32)
    with pytest.raises(JudgmentInvalid, match="sample ID"):
        validate_judgment(wrong_id, sentinel["trace"], sentinel)

    wrong_quote = json.loads(json.dumps(valid))
    wrong_quote["evidence"][0]["quote"] = "quote absent from trace"
    with pytest.raises(JudgmentInvalid, match="quote"):
        validate_judgment(wrong_quote, sentinel["trace"], sentinel)

    wrong_applicability = dict(valid, obsolete_acceptance="not_applicable")
    with pytest.raises(JudgmentInvalid, match="applicability"):
        validate_judgment(wrong_applicability, sentinel["trace"], sentinel)


@pytest.mark.asyncio
async def test_scorer_retries_invalid_and_persists_actual_usage_once_valid() -> None:
    sentinel = _sentinel()
    invalid = json.loads(json.dumps(sentinel["expected_judgment"]))
    invalid["evidence"][0]["quote"] = "not present"
    transport = FakeTransport([invalid, sentinel["expected_judgment"]])

    record = await BehavioralScorer(transport, max_attempts=2).score(sentinel, sentinel["trace"])

    assert record["call_status"] == "ok"
    assert record["attempts"] == 2
    assert record["request_id"] == "judge-2"
    assert record["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert record["judge_output"] == sentinel["expected_judgment"]
    assert len(record["input_sha256"]) == 64
    assert len(record["prompt_sha256"]) == 64
    assert len(record["schema_sha256"]) == 64
    assert "validation_feedback" not in transport.calls[0]["user_payload"]
    feedback = transport.calls[1]["user_payload"]["validation_feedback"]
    assert feedback["previous_output"] == invalid
    assert "evidence quote not found" in feedback["error"]
    assert feedback["instruction"] == "Correct the previous output and satisfy the same strict schema."
    quote_examples = feedback["valid_quote_examples_by_source"]
    assert set(quote_examples) == {
        event["source"]
        for event in sentinel["trace"]
        if event["source"] in {"assistant_answer", "tool_plan", "tool_call", "final_answer"}
    }
    assert any("8080" in quote for quote in quote_examples["assistant_answer"])
    assert all(1 <= len(quote) <= 160 for quotes in quote_examples.values() for quote in quotes)
    retry_quote_schema = transport.calls[1]["response_schema"]["properties"]["evidence"]["items"]["properties"]["quote"]
    assert set(retry_quote_schema["enum"]) == {quote for quotes in quote_examples.values() for quote in quotes}


@pytest.mark.asyncio
async def test_scorer_schema_enforces_frozen_applicability() -> None:
    sentinel = _sentinel(4)
    transport = FakeTransport([sentinel["expected_judgment"]])

    await BehavioralScorer(transport).score(sentinel, sentinel["trace"])

    schema = transport.calls[0]["response_schema"]
    assert schema["properties"]["stable_fact_disposition"]["enum"] == [
        "accepted",
        "accepted_with_noninvalidating_caveat",
        "ignored",
        "rejected_as_stale",
        "overwritten_due_to_staleness",
        "unclear",
    ]
    assert schema["properties"]["obsolete_acceptance"]["enum"] == ["not_applicable"]
    assert schema["properties"]["verification_action"]["enum"] == ["not_applicable"]
    assert schema["properties"]["final_attribution"]["enum"] == ["not_applicable"]
    assert schema["properties"]["unsupported_new_configuration"]["enum"] == ["not_applicable"]


@pytest.mark.asyncio
async def test_scorer_schema_requires_unclear_for_an_empty_trace() -> None:
    sentinel = _sentinel(8)
    transport = FakeTransport([sentinel["expected_judgment"]])

    await BehavioralScorer(transport).score(sentinel, sentinel["trace"])

    schema = transport.calls[0]["response_schema"]
    for dimension in sentinel["applicable_dimensions"]:
        assert schema["properties"][dimension]["enum"] == ["unclear"]
    assert schema["properties"]["confidence"]["enum"] == ["low"]
    assert schema["properties"]["review_reason"]["enum"] == ["missing_trace"]
    assert schema["properties"]["evidence"]["maxItems"] == 0


@pytest.mark.asyncio
async def test_scorer_rejects_a_transport_response_that_violates_the_specialized_schema() -> None:
    sentinel = _sentinel(8)
    invalid = json.loads(json.dumps(sentinel["expected_judgment"]))
    invalid["confidence"] = "high"
    invalid["review_reason"] = "rubric_boundary"
    transport = FakeTransport([invalid, sentinel["expected_judgment"]])

    record = await BehavioralScorer(transport, max_attempts=2).score(sentinel, sentinel["trace"])

    assert record["call_status"] == "ok"
    assert record["attempts"] == 2
    assert record["judge_output"] == sentinel["expected_judgment"]


@pytest.mark.asyncio
async def test_scorer_exhaustion_is_invalid_not_all_false() -> None:
    sentinel = _sentinel()
    invalid = json.loads(json.dumps(sentinel["expected_judgment"]))
    invalid["sample_id"] = "f" * 32
    transport = FakeTransport([invalid, invalid])

    record = await BehavioralScorer(transport, max_attempts=2).score(sentinel, sentinel["trace"])

    assert record["call_status"] == "invalid"
    assert record["attempts"] == 2
    assert record["judge_output"] is None
    assert "sample ID" in record["error"]


class ConcurrencyTransport(httpx.AsyncBaseTransport):
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.active = 0
        self.maximum_active = 0
        self.payloads: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.payloads.append(json.loads(request.content))
        await asyncio.sleep(0.01)
        self.active -= 1
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "request-compatible",
                "choices": [{"message": {"content": json.dumps(self.output)}}],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            },
        )


class InvalidThenValidTransport(httpx.AsyncBaseTransport):
    def __init__(self, valid_output: dict[str, Any]) -> None:
        self.valid_output = valid_output
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        content = "not-json" if self.calls == 1 else json.dumps(self.valid_output)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


@pytest.mark.asyncio
async def test_transport_does_not_hide_billable_parse_retry_from_budget_wrapper() -> None:
    sentinel = _sentinel()
    wire = InvalidThenValidTransport(sentinel["expected_judgment"])
    async with httpx.AsyncClient(transport=wire) as client:
        transport = CompatibleStructuredTransport(
            "secret",
            client=client,
            max_http_attempts=3,
            backoff_seconds=0,
        )
        with pytest.raises(json.JSONDecodeError):
            await transport.complete(
                system_prompt="judge",
                user_payload={"sample_id": sentinel["opaque_sample_id"]},
                schema_name="judge",
                response_schema=JUDGE_SCHEMA,
                max_output_tokens=600,
            )

    assert wire.calls == 1


@pytest.mark.asyncio
async def test_compatible_transport_freezes_snapshot_strict_schema_and_concurrency() -> None:
    sentinel = _sentinel()
    wire = ConcurrencyTransport(sentinel["expected_judgment"])
    async with httpx.AsyncClient(transport=wire) as client:
        transport = CompatibleStructuredTransport("secret", client=client, concurrency=8)
        results = await asyncio.gather(
            *[
                transport.complete(
                    system_prompt="judge",
                    user_payload={"sample_id": sentinel["opaque_sample_id"]},
                    schema_name="judge",
                    response_schema=JUDGE_SCHEMA,
                    max_output_tokens=600,
                )
                for _ in range(12)
            ]
        )

    assert wire.maximum_active == 8
    assert all(result.input_tokens == 11 and result.output_tokens == 7 for result in results)
    payload = wire.payloads[0]
    assert payload["model"] == MODEL_SNAPSHOT
    assert payload["enable_thinking"] is False
    assert payload["temperature"] == 0
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "judge", "schema": JUDGE_SCHEMA, "strict": True},
    }
