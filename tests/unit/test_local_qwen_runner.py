from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from hl_mem.evaluation.local_qwen_runner import (
    LocalQwenRunner,
    ModelResponseError,
    OversizedDocket,
    QwenRunConfig,
    UnsafeModelPayload,
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def __call__(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((url, payload))
        body = json.loads(payload["messages"][-1]["content"])
        if body["task"] == "evidence_card":
            return {
                "summary": "bounded card",
                "evidence_ids": [item.get("evidence_id") for item in body["input"]["evidence"]],
                "chain_of_thought": "must never persist",
            }
        return {
            "decision": "keep_left",
            "winner_candidate_key": "a",
            "confidence": 0.95,
            "rationale_code": "strict_test",
            "thinking": "must never persist",
        }


def _declared_tokens(text: str) -> int:
    value = json.loads(text)
    input_payload = value["input"]
    if "_token_cost" in input_payload:
        return int(input_payload["_token_cost"])
    return sum(int(item.get("tokens", 0)) for item in input_payload.get("evidence", []))


def _case(**extra: object) -> dict[str, object]:
    return {
        "case_id": "case-1",
        "candidates": [{"candidate_key": "a"}, {"candidate_key": "b"}],
        "evidence": [],
        **extra,
    }


def test_config_is_fixed_to_loopback_non_thinking_safety_envelope() -> None:
    config = QwenRunConfig()

    assert config.base_url is None
    assert config.source_dir == "D:/qwen38-local/"
    assert config.prompt_version == "v030-judge-v1"
    assert config.tokenizer_identity == "qwen3.8-gguf-embedded"
    assert config.context_window == 16384
    assert config.max_input_tokens == 11500
    assert config.max_output_tokens == 1024
    assert config.max_chunk_tokens == 7500
    assert config.max_card_calls == 4
    assert config.max_calls == 6
    assert config.enable_thinking is False

    with pytest.raises(ValueError, match="loopback"):
        QwenRunConfig(base_url="https://example.com/v1")
    with pytest.raises(ValueError, match="enable_thinking"):
        QwenRunConfig(enable_thinking=True)


def test_http_transport_requires_an_explicit_base_url() -> None:
    with pytest.raises(ValueError, match="base_url is required when using HTTP transport"):
        LocalQwenRunner(token_counter=_declared_tokens)

    runner = LocalQwenRunner(
        token_counter=_declared_tokens,
        config=QwenRunConfig(base_url="http://127.0.0.1:18090/v1"),
    )

    assert runner.config.base_url == "http://127.0.0.1:18090/v1"


def test_runner_disables_thinking_and_reverses_candidate_order() -> None:
    transport = FakeTransport()
    runner = LocalQwenRunner(transport=transport, token_counter=_declared_tokens)

    result = runner.run_case(_case(_token_cost=11500))

    assert result["consistent"] is True
    assert result["decision"] == "keep_left"
    assert result["call_count"] == 2
    first = json.loads(transport.calls[0][1]["messages"][-1]["content"])
    second = json.loads(transport.calls[1][1]["messages"][-1]["content"])
    assert [item["candidate_key"] for item in first["input"]["candidates"]] == ["a", "b"]
    assert [item["candidate_key"] for item in second["input"]["candidates"]] == ["b", "a"]
    for _, payload in transport.calls:
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 1024
        assert payload["metadata"]["input_tokens"] == 11500
    serialized = json.dumps(result, ensure_ascii=False)
    assert "thinking" not in serialized
    assert "chain_of_thought" not in serialized


def test_runner_rejects_gold_and_cot_before_transport() -> None:
    transport = FakeTransport()
    runner = LocalQwenRunner(transport=transport, token_counter=_declared_tokens)

    with pytest.raises(UnsafeModelPayload, match="gold"):
        runner.run_case(_case(gold={"decision": "keep_left"}))
    with pytest.raises(UnsafeModelPayload, match="chain_of_thought"):
        runner.run_case(_case(chain_of_thought="hidden"))

    assert transport.calls == []


def test_runner_fails_before_transport_above_final_input_limit() -> None:
    transport = FakeTransport()
    runner = LocalQwenRunner(transport=transport, token_counter=_declared_tokens)

    with pytest.raises(OversizedDocket, match="11500"):
        runner.run_case(_case(_token_cost=11501))

    assert transport.calls == []


def test_four_evidence_chunks_plus_two_decisions_hit_six_call_limit_without_truncation() -> None:
    transport = FakeTransport()
    runner = LocalQwenRunner(transport=transport, token_counter=_declared_tokens)
    evidence = [{"evidence_id": f"e{index}", "tokens": 7400} for index in range(4)]

    result = runner.run_case(_case(evidence=evidence))

    assert result["call_count"] == 6
    assert [call[1]["metadata"]["input_tokens"] for call in transport.calls[:4]] == [7400] * 4
    card_inputs = [
        item
        for _, request in transport.calls[:4]
        for item in json.loads(request["messages"][-1]["content"])["input"]["evidence"]
    ]
    assert [item["evidence_id"] for item in card_inputs] == ["e0", "e1", "e2", "e3"]


@pytest.mark.parametrize(
    "evidence",
    [
        [
            {"evidence_id": "too-large", "tokens": 7501},
            {"evidence_id": "forces-card-path", "tokens": 4000},
        ],
        [{"evidence_id": f"e{index}", "tokens": 7400} for index in range(5)],
    ],
)
def test_evidence_chunking_fails_closed_for_oversize_or_more_than_four_cards(evidence: list[dict]) -> None:
    transport = FakeTransport()
    runner = LocalQwenRunner(transport=transport, token_counter=_declared_tokens)

    with pytest.raises(OversizedDocket):
        runner.run_case(_case(evidence=evidence))

    assert transport.calls == []


def test_order_disagreement_is_returned_as_manual_required() -> None:
    calls = 0

    def positional_transport(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        decision = "keep_left" if calls == 1 else "keep_right"
        return {"decision": decision, "confidence": 0.99, "rationale_code": "positional"}

    runner = LocalQwenRunner(transport=positional_transport, token_counter=_declared_tokens)

    result = runner.run_case(_case(_token_cost=1))

    assert result["consistent"] is False
    assert result["decision"] == "manual_required"
    assert result["failure_reason"] == "candidate_order_disagreement"


def _openai_response(content: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


def test_runner_extracts_fenced_json_without_changing_decision_values() -> None:
    content = """prefix\n```json
{"decision":"keep_left","winner_candidate_key":"a","confidence":0.97,"rationale_code":"fixture"}
```\nsuffix"""
    runner = LocalQwenRunner(
        transport=lambda _url, _payload: _openai_response(content),
        token_counter=_declared_tokens,
    )

    result = runner.run_case(_case(_token_cost=1))

    assert result["decision"] == "keep_left"
    assert result["winner_candidate_key"] == "a"
    assert result["call_count"] == 2


def test_runner_maps_only_registered_field_aliases() -> None:
    content = json.dumps(
        {
            "decision": "keep_right",
            "winner_key": "b",
            "confidence": 0.96,
            "rationale": "fixture_alias",
            "evidence_ids": ["e-1"],
            "ambiguities": [],
        }
    )
    runner = LocalQwenRunner(
        transport=lambda _url, _payload: _openai_response(content),
        token_counter=_declared_tokens,
    )

    result = runner.run_case(_case(_token_cost=1))

    assert result["decisions"][0] == {
        "decision": "keep_right",
        "winner_candidate_key": "b",
        "confidence": 0.96,
        "rationale_code": "fixture_alias",
        "decisive_evidence_ids": ["e-1"],
        "ambiguity_flags": [],
    }


def test_runner_retries_malformed_structure_once_as_format_repair() -> None:
    tasks: list[str] = []

    def repair_transport(_url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.loads(payload["messages"][-1]["content"])
        tasks.append(body["task"])
        if body["task"] == "decision":
            return _openai_response("not-json")
        return _openai_response(
            '{"decision":"keep_left","winner_candidate_key":"a","confidence":0.95,"rationale_code":"repaired"}'
        )

    runner = LocalQwenRunner(transport=repair_transport, token_counter=_declared_tokens)

    result = runner.run_case(_case(_token_cost=1))

    assert tasks == ["decision", "format_repair", "decision_verify"]
    assert result["decision"] == "keep_left"
    assert result["call_count"] == 3


def test_runner_stops_after_one_failed_format_repair() -> None:
    tasks: list[str] = []

    def malformed_transport(_url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.loads(payload["messages"][-1]["content"])
        tasks.append(body["task"])
        return _openai_response("still-not-json")

    runner = LocalQwenRunner(transport=malformed_transport, token_counter=_declared_tokens)

    with pytest.raises(ModelResponseError):
        runner.run_case(_case(_token_cost=1))

    assert tasks == ["decision", "format_repair"]


def test_cards_that_still_do_not_fit_final_docket_fail_as_infrastructure_error() -> None:
    transport = FakeTransport()

    def final_docket_counter(text: str) -> int:
        value = json.loads(text)["input"]
        if "evidence_cards" in value:
            return 11501
        return sum(int(item.get("tokens", 0)) for item in value.get("evidence", []))

    runner = LocalQwenRunner(transport=transport, token_counter=final_docket_counter)
    evidence = [{"evidence_id": "left", "tokens": 6000}, {"evidence_id": "right", "tokens": 6000}]

    with pytest.raises(OversizedDocket, match="final docket") as captured:
        runner.run_case(_case(evidence=evidence))

    assert captured.value.reason == "oversized_docket"
    assert len(transport.calls) == 2


def test_runner_preserves_only_bounded_batch_predictions() -> None:
    response = {
        "decision": "batch",
        "confidence": 1.0,
        "predictions": [{"case_id": "c1", "decision": "equivalent", "confidence": 0.98, "thinking": "drop"}],
    }
    runner = LocalQwenRunner(transport=lambda _url, _payload: response, token_counter=_declared_tokens)

    result = runner.run_case(_case(_token_cost=1))

    assert result["decisions"][0]["predictions"] == [{"case_id": "c1", "decision": "equivalent", "confidence": 0.98}]


def test_runner_expands_registered_compact_prediction_keys() -> None:
    response = {
        "decision": "batch",
        "confidence": 1.0,
        "predictions": [{"id": "c1", "i": "high", "s": "explicit_correction", "r": "temporal", "c": 0.9}],
    }
    runner = LocalQwenRunner(transport=lambda _url, _payload: response, token_counter=_declared_tokens)

    result = runner.run_case(_case(_token_cost=1))

    assert result["decisions"][0]["predictions"] == [
        {
            "case_id": "c1",
            "importance": "high",
            "lesson_signal": "explicit_correction",
            "retention": "temporal",
            "confidence": 0.9,
        }
    ]
