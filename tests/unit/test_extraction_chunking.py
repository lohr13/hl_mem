"""长输入结构感知分块与输出超限恢复测试。"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from hl_mem.errors import LLMOutputTruncatedError
from hl_mem.ingest.chunking import (
    ChunkingPolicy,
    ContentStructure,
    bisect_extraction_chunk,
    detect_content_structure,
    split_extraction_content,
)
from hl_mem.ingest.extraction import parsing
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.types import LLMRequest, LLMResponse
from hl_mem.observability.audit import audit_scope


class _SequenceClient:
    """按顺序返回预设 LLM 响应。"""

    class _Provider:
        """最小 provider 标识。"""

        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        """记录请求并返回下一个预设响应。"""
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def emit(self, phase, action, outcome, *, detail=None, **_dimensions):
        self.events.append((phase, action, outcome, detail or {}))
        return True


def _compact_fact(value: str) -> dict[str, object]:
    return {
        "subject": "user",
        "value": value,
        "kind": "fact",
        "confidence": 1.0,
        "notability": "high",
        "evidence_quote": value,
        "source_event_indices": [0],
    }


def _compact_response(values: list[str]) -> str:
    return json.dumps(
        {"claims": [_compact_fact(value) for value in values], "should_memorize": True},
        ensure_ascii=False,
    )


def test_claim_budget_ranks_compact_claims_without_mutating_input() -> None:
    claims = []
    for index in range(16):
        claim = _compact_fact(f"low-{index}")
        claim.update({"notability": "low", "confidence": 0.9})
        claims.append(claim)
    high = _compact_fact("high")
    high.update({"notability": "high", "confidence": 0.1})
    medium = _compact_fact("medium")
    medium.update({"notability": "medium", "confidence": 0.2})
    payload = {"claims": [*claims, high, medium], "should_memorize": True}
    original = deepcopy(payload)

    result = parsing.cap_extraction_claims(payload, limit=16)

    assert result.generated_count == 18
    assert result.retained_count == 16
    assert result.dropped_count == 2
    assert [claim["value"] for claim in result.payload["claims"]] == [
        "high",
        "medium",
        *(f"low-{index}" for index in range(14)),
    ]
    assert payload == original


def test_claim_budget_uses_confidence_then_original_order_for_ties() -> None:
    claims = []
    for index in range(18):
        claim = _compact_fact(f"claim-{index}")
        claim.update({"notability": "medium", "confidence": 0.5})
        claims.append(claim)
    claims[-1]["confidence"] = "0.9"

    result = parsing.cap_extraction_claims({"claims": claims}, limit=16)

    assert [claim["value"] for claim in result.payload["claims"]] == [
        "claim-17",
        *(f"claim-{index}" for index in range(15)),
    ]


def test_claim_budget_uses_confidence_and_order_when_notability_is_missing() -> None:
    claims = [{"value": f"claim-{index}", "importance": 0.9, "confidence": 0.5} for index in range(17)]
    claims[-1]["importance"] = 0.0
    claims[-1]["confidence"] = 0.9

    result = parsing.cap_extraction_claims({"claims": claims}, limit=16)

    assert result.payload["claims"][0]["value"] == "claim-16"
    assert "claim-15" not in {claim["value"] for claim in result.payload["claims"]}


def test_claim_budget_drops_malformed_notability_instead_of_promoting_importance() -> None:
    claims = []
    for index in range(16):
        claim = _compact_fact(f"valid-{index}")
        claim.update({"notability": "low", "confidence": 0.1})
        claims.append(claim)
    malformed = _compact_fact("malformed")
    malformed.update({"notability": ["bogus"], "confidence": 1.0, "importance": 999})

    result = parsing.cap_extraction_claims({"claims": [*claims, malformed]}, limit=16)

    assert [claim["value"] for claim in result.payload["claims"]] == [f"valid-{index}" for index in range(16)]


def test_claim_budget_leaves_non_list_for_validation_and_rejects_invalid_limit() -> None:
    payload = {"claims": "invalid", "should_memorize": True}

    result = parsing.cap_extraction_claims(payload)

    assert result.payload == payload
    assert result.generated_count == result.retained_count == result.dropped_count == 0
    with pytest.raises(ValueError, match="positive"):
        parsing.cap_extraction_claims(payload, limit=0)


def _full_response(values: list[str]) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "subject": "user",
                    "predicate": "记录",
                    "canonical_attribute": "custom.unknown",
                    "value": value,
                    "qualifiers": {},
                    "confidence": 1.0,
                    "volatility": "stable",
                    "reason": "explicit statement",
                    "scope": "permanent",
                    "importance": 0.8,
                    "source_event_indices": [0],
                }
                for value in values
            ],
            "entities": [],
            "should_memorize": True,
            "sensitivity": "normal",
        },
        ensure_ascii=False,
    )


def test_short_text_uses_single_chunk() -> None:
    """短文本保持单块快速路径。"""
    chunks = split_extraction_content("短输入", ChunkingPolicy(100, 1, 2))

    assert len(chunks) == 1
    assert chunks[0].text == "短输入"
    assert chunks[0].context_prefix == ""
    assert chunks[0].structure is ContentStructure.TEXT


def test_conversation_preserves_turn_order_and_overlap_is_context_only() -> None:
    """对话分块保持 turn 顺序；仅超长 turn 可拆，重叠 turn 只作上下文。"""
    content = {
        "messages": [
            {"role": "user", "content": "a" * 12},
            {"role": "assistant", "content": "b" * 12},
            {"role": "user", "content": "c" * 12},
        ]
    }

    chunks = split_extraction_content(content, ChunkingPolicy(55, 1, 2))

    assert detect_content_structure(content) is ContentStructure.CONVERSATION
    assert len(chunks) >= 2
    extracted_turns = [json.loads(line) for chunk in chunks for line in chunk.text.splitlines() if line]
    assert extracted_turns == content["messages"]
    assert json.loads(chunks[1].context_prefix.splitlines()[-1]) in content["messages"][:2]
    assert chunks[1].context_prefix not in chunks[1].text


def test_jsonl_preserves_object_lines() -> None:
    """JSONL 分块始终保留完整对象行。"""
    lines = [json.dumps({"index": index, "value": "x" * 20}) for index in range(4)]
    chunks = split_extraction_content("\n".join(lines), ChunkingPolicy(60, 0, 2))

    assert chunks[0].structure is ContentStructure.JSONL
    assert [json.loads(line) for chunk in chunks for line in chunk.text.splitlines()] == [
        json.loads(line) for line in lines
    ]


def test_large_conversation_contains_each_turn_once_as_extractable_content() -> None:
    """大量对话 turn 在主提取内容中不丢失且不重复。"""
    turns = [{"role": "user", "content": f"turn-{index}"} for index in range(100)]
    chunks = split_extraction_content(
        {"messages": turns},
        ChunkingPolicy(250, 2, 3),
    )

    extracted_turns = [json.loads(line) for chunk in chunks for line in chunk.text.splitlines() if line.strip()]
    assert extracted_turns == turns


def test_oversized_conversation_turn_is_split_into_bounded_json_units() -> None:
    """单个超长 turn 也必须保持 JSON 可解析并服从字符上限。"""
    target_chars = 500
    original = ("COVID notes with mortality figures. " * 600).strip()

    chunks = split_extraction_content(
        {"messages": [{"role": "user", "content": original}]},
        ChunkingPolicy(target_chars, 0, 3),
    )

    units = [line for chunk in chunks for line in chunk.text.splitlines() if line]
    fragments = [json.loads(unit) for unit in units]
    assert len(chunks) > 1
    assert all(len(chunk.text) <= target_chars for chunk in chunks)
    assert all(len(unit) <= target_chars for unit in units)
    assert all(fragment["role"] == "user" for fragment in fragments)
    assert "".join(str(fragment["content"]) for fragment in fragments) == original


def test_text_prefers_paragraph_boundaries_and_can_be_bisected() -> None:
    """普通文本优先按段落切块，生成块仍可继续二分。"""
    content = "第一段。" * 8 + "\n\n" + "第二段。" * 8
    chunks = split_extraction_content(content, ChunkingPolicy(30, 0, 2))
    split = bisect_extraction_chunk(chunks[0])

    assert len(chunks) >= 2
    assert split is not None
    assert split[0].text + split[1].text == chunks[0].text


def test_exact_compact_limit_keeps_single_request_with_deprecated_flags_enabled() -> None:
    """Deprecated split flags must not add requests at the hard budget."""
    values = [f"User recorded baseline item {index:02d}" for index in range(24)]
    client = _SequenceClient([LLMResponse(_compact_response(values), "stop", 10)])
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
            delta_repair_enabled=True,
        ).extract("\n".join(values))

    assert len(client.requests) == 1
    assert {claim.value for claim in claims} == set(values)
    assert not [event for event in audit.events if event[1] in {"claim_budget", "possible_under_extraction"}]


def test_legacy_schema_overflow_uses_the_same_hard_budget() -> None:
    """Compatible legacy responses use the same deterministic hard cap."""
    values = [f"User recorded legacy item {index:02d}" for index in range(28)]
    client = _SequenceClient([LLMResponse(_full_response(values), "stop", 10)])
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
        ).extract("\n".join(values))

    assert len(client.requests) == 1
    assert [claim.value for claim in claims] == values[:24]
    assert [event[3]["dropped_claim_count"] for event in audit.events if event[2] == "overflow_truncated"] == [4]


def test_truncated_output_is_bisected_and_usage_is_accumulated() -> None:
    """输出截断后二分重试，累计全部请求 token 并稳定去重。"""
    claim = {
        "subject": "用户",
        "predicate": "偏好",
        "canonical_attribute": "preference.ui_theme",
        "value": "深色模式",
        "qualifiers": {},
        "confidence": 0.9,
        "volatility": "stable",
        "reason": "明确陈述",
        "scope": "permanent",
        "importance": 0.8,
    }
    valid = json.dumps({"claims": [claim], "should_memorize": True})
    client = _SequenceClient(
        [
            LLMResponse('{"claims":[', "length", 10),
            LLMResponse(valid, "stop", 11),
            LLMResponse(valid, "stop", 12),
        ]
    )
    extractor = LLMExtractor(
        client,
        ChunkingPolicy(1_000, 0, 2),
    )

    claims = extractor.extract("第一段内容：用户偏好深色模式。\n\n第二段内容：用户偏好深色模式。")

    assert len(client.requests) == 3
    assert len(claims) == 1
    assert extractor.last_usage_tokens == 33


def test_claim_count_overflow_is_ranked_and_truncated_without_another_request() -> None:
    low_claims = []
    for index in range(28):
        claim = _compact_fact(f"low-{index}")
        claim.update({"notability": "low", "confidence": 0.9})
        low_claims.append(claim)
    high = _compact_fact("high")
    high.update({"notability": "high", "confidence": 0.1})
    medium = _compact_fact("medium")
    medium.update({"notability": "medium", "confidence": 0.2})
    overflow = json.dumps({"claims": [*low_claims, high, medium], "should_memorize": True})
    client = _SequenceClient([LLMResponse(overflow, "stop", 10)])
    audit = _RecordingAudit()
    extractor = LLMExtractor(
        client,
        ChunkingPolicy(1_000, 0, 2),
        schema_retries=2,
        soft_split_enabled=True,
        delta_repair_enabled=True,
    )

    with audit_scope(audit):
        claims = extractor.extract("\n".join([*(f"low-{index}" for index in range(24)), "high", "medium"]))

    assert [claim.value for claim in claims] == [
        "high",
        "medium",
        *(f"low-{index}" for index in range(22)),
    ]
    assert len(client.requests) == 1
    assert extractor._schema_retry_count == 0
    assert extractor.last_usage_tokens == 10
    assert [event for event in audit.events if event[2] == "overflow_truncated"] == [
        (
            "extract",
            "claim_budget",
            "overflow_truncated",
            {
                "generated_claim_count": 30,
                "retained_claim_count": 24,
                "dropped_claim_count": 6,
                "chunk_index": 0,
                "start_unit": 0,
                "end_unit": 1,
            },
        )
    ]


def test_malformed_overflow_is_audited_only_after_schema_retry_succeeds() -> None:
    first_claims = [_compact_fact(f"first-{index}") for index in range(25)]
    first_claims[0].pop("subject")
    second_values = [f"second-{index}" for index in range(25)]
    client = _SequenceClient(
        [
            LLMResponse(
                json.dumps({"claims": first_claims, "should_memorize": True}),
                "stop",
                10,
            ),
            LLMResponse(_compact_response(second_values), "stop", 11),
        ]
    )
    audit = _RecordingAudit()
    extractor = LLMExtractor(client, ChunkingPolicy(1_000, 0, 2), schema_retries=1)

    with audit_scope(audit):
        claims = extractor.extract("\n".join(second_values))

    assert [claim.value for claim in claims] == second_values[:24]
    assert len(client.requests) == 2
    assert extractor._schema_retry_count == 1
    assert [event for event in audit.events if event[2] == "overflow_truncated"] == [
        (
            "extract",
            "claim_budget",
            "overflow_truncated",
            {
                "generated_claim_count": 25,
                "retained_claim_count": 24,
                "dropped_claim_count": 1,
                "chunk_index": 0,
                "start_unit": 0,
                "end_unit": 1,
            },
        )
    ]


def test_truncation_at_max_depth_reports_chunk_location() -> None:
    """达到递归上限时错误包含 chunk 范围与深度。"""
    client = _SequenceClient([LLMResponse("", "max_tokens", 7)])
    extractor = LLMExtractor(
        client,
        ChunkingPolicy(1_000, 0, 0),
    )

    try:
        extractor.extract("会被截断的内容")
    except LLMOutputTruncatedError as error:
        assert "chunk=0" in str(error)
        assert "start_unit=0" in str(error)
        assert "depth=0" in str(error)
    else:
        raise AssertionError("expected LLMOutputTruncatedError")
