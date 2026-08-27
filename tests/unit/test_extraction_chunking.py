"""长输入结构感知分块与输出超限恢复测试。"""

from __future__ import annotations

import json

from hl_mem.errors import LLMOutputTruncatedError
from hl_mem.ingest.chunking import (
    ChunkingPolicy,
    ContentStructure,
    bisect_extraction_chunk,
    detect_content_structure,
    split_extraction_content,
)
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


def _balanced_two_paragraph_source(left_values: list[str], right_values: list[str]) -> str:
    left = "\n".join(left_values)
    right = "\n".join(right_values)
    return left + "\n\n" + right + ("x" * max(0, len(left) - len(right)))


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


def test_exact_compact_limit_keeps_single_request_when_soft_split_is_disabled() -> None:
    """默认关闭必须保持 compact==20 的现有单请求行为。"""
    values = [f"User recorded baseline item {index:02d}" for index in range(20)]
    client = _SequenceClient([LLMResponse(_compact_response(values), "stop", 10)])
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(client, ChunkingPolicy(10_000, 0, 3)).extract("\n".join(values))

    assert len(client.requests) == 1
    assert {claim.value for claim in claims} == set(values)
    assert [event[2] for event in audit.events if event[1] == "possible_under_extraction"] == ["claim_limit_reached"]


def test_soft_split_preserves_root_merges_children_and_does_not_recurse_on_residual() -> None:
    """软触发只拆一层，保留根结果并用既有 merge 去重。"""
    left_values = [f"User recorded left item {index:02d}" for index in range(20)]
    right_values = [f"User recorded right item {index:02d}" for index in range(20)]
    root_values = left_values[:10] + right_values[:10]
    client = _SequenceClient(
        [
            LLMResponse(_compact_response(root_values), "stop", 10),
            LLMResponse(_compact_response(left_values), "stop", 11),
            LLMResponse(_compact_response(right_values), "stop", 12),
        ]
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        extractor = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
        )
        claims = extractor.extract("\n".join(left_values + right_values))

    assert extractor.delta_repair_enabled is False
    assert len(client.requests) == 3
    assert [claim.value for claim in claims[:20]] == root_values
    assert {claim.value for claim in claims} == set(left_values + right_values)
    outcomes = [event[2] for event in audit.events]
    assert outcomes.count("claim_limit_residual_after_split") == 2
    assert outcomes.count("claim_limit_split_applied") == 1
    split_detail = next(event[3] for event in audit.events if event[2] == "claim_limit_split_applied")
    assert split_detail == {
        "claim_count_before_split": 20,
        "root_unique_claim_count": 20,
        "left_claim_count": 20,
        "right_claim_count": 20,
        "merged_claim_count": 40,
        "net_new_after_split": 20,
        "duplicates_removed": 20,
        "chunk_index": 0,
        "start_unit": 0,
        "end_unit": 1,
        "source_length": len("\n".join(left_values + right_values)),
    }
    assert "delta_repair_applied" not in outcomes


def test_delta_repair_runs_once_for_residual_and_merges_only_new_claims() -> None:
    """残余子块只补一轮，并在既有五元组 merge 后报告真实净新增。"""
    left_values = [f"User recorded left item {index:02d}" for index in range(21)]
    right_values = [f"User recorded right item {index:02d}" for index in range(4)]
    root_values = left_values[:16] + right_values
    client = _SequenceClient(
        [
            LLMResponse(_compact_response(root_values), "stop", 10),
            LLMResponse(_compact_response(left_values[:20]), "stop", 11),
            LLMResponse(_compact_response([left_values[0], left_values[20]]), "stop", 12),
            LLMResponse(_compact_response(right_values), "stop", 13),
        ]
    )
    audit = _RecordingAudit()
    source = _balanced_two_paragraph_source(left_values, right_values)

    with audit_scope(audit):
        claims = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
            delta_repair_enabled=True,
        ).extract(source)

    assert len(client.requests) == 4
    assert {claim.value for claim in claims} == set(left_values + right_values)
    repair_prompt = client.requests[2].messages[1].content
    assert left_values[20] in repair_prompt
    assert f"1. user | {left_values[0]}" in repair_prompt
    assert "Extract only new atomic facts not covered by the list above" in repair_prompt
    outcomes = [event[2] for event in audit.events]
    assert outcomes.count("claim_limit_residual_after_split") == 1
    assert outcomes.count("delta_repair_applied") == 1
    repair_detail = next(event[3] for event in audit.events if event[2] == "delta_repair_applied")
    assert repair_detail == {
        "residual_claim_count": 20,
        "repair_new_count": 2,
        "merged_total": 21,
        "net_new_after_repair": 1,
        "duplicates_removed": 1,
        "chunk_index": 0,
        "start_unit": 0,
        "end_unit": 1,
        "source_length": len("\n".join(left_values) + "\n\n"),
        "repair_status": "success",
    }


def test_delta_repair_empty_response_stops_after_one_request() -> None:
    """合法空 repair 响应立即停止，保留软拆分已有 claims。"""
    left_values = [f"User recorded left item {index:02d}" for index in range(20)]
    right_values = ["User recorded right item"]
    root_values = left_values[:19] + right_values
    empty_response = json.dumps({"claims": [], "should_memorize": False})
    client = _SequenceClient(
        [
            LLMResponse(_compact_response(root_values), "stop", 10),
            LLMResponse(_compact_response(left_values), "stop", 11),
            LLMResponse(empty_response, "stop", 12),
            LLMResponse(_compact_response(right_values), "stop", 13),
        ]
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
            delta_repair_enabled=True,
        ).extract(_balanced_two_paragraph_source(left_values, right_values))

    assert len(client.requests) == 4
    assert {claim.value for claim in claims} == set(left_values + right_values)
    repair_detail = next(event[3] for event in audit.events if event[2] == "delta_repair_applied")
    assert repair_detail["repair_new_count"] == 0
    assert repair_detail["net_new_after_repair"] == 0
    assert not [event for event in audit.events if event[2] == "claim_limit_residual_after_repair"]


def test_delta_repair_saturation_emits_residual_without_recursing() -> None:
    """repair 再次返回 20 条时仅审计，不发第二轮请求。"""
    left_values = [f"User recorded left item {index:02d}" for index in range(40)]
    right_values = ["User recorded right item"]
    root_values = left_values[:19] + right_values
    client = _SequenceClient(
        [
            LLMResponse(_compact_response(root_values), "stop", 10),
            LLMResponse(_compact_response(left_values[:20]), "stop", 11),
            LLMResponse(_compact_response(left_values[20:]), "stop", 12),
            LLMResponse(_compact_response(right_values), "stop", 13),
        ]
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
            delta_repair_enabled=True,
        ).extract(_balanced_two_paragraph_source(left_values, right_values))

    assert len(client.requests) == 4
    residual = [event for event in audit.events if event[2] == "claim_limit_residual_after_repair"]
    assert len(residual) == 1
    assert residual[0][3]["claim_count"] == 20
    assert [event[2] for event in audit.events].count("delta_repair_applied") == 1


def test_delta_repair_failure_preserves_residual_claims_and_continues() -> None:
    """repair 调用失败必须 fail-open，右子块仍继续提取且根 claims 不丢。"""
    left_values = [f"User recorded left item {index:02d}" for index in range(20)]
    right_values = ["User recorded right item"]
    root_values = left_values[:19] + right_values
    client = _SequenceClient(
        [
            LLMResponse(_compact_response(root_values), "stop", 10),
            LLMResponse(_compact_response(left_values), "stop", 11),
            RuntimeError("repair unavailable"),
            LLMResponse(_compact_response(right_values), "stop", 13),
        ]
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
            delta_repair_enabled=True,
        ).extract(_balanced_two_paragraph_source(left_values, right_values))

    assert len(client.requests) == 4
    assert {claim.value for claim in claims} == set(left_values + right_values)
    repair_detail = next(event[3] for event in audit.events if event[2] == "delta_repair_applied")
    assert repair_detail["repair_status"] == "failed"
    assert repair_detail["repair_new_count"] == 0
    assert repair_detail["net_new_after_repair"] == 0
    assert repair_detail["error_class"] == "RuntimeError"


def test_hard_recovery_inside_soft_child_does_not_reset_one_level_guard() -> None:
    """软拆子块即使发生硬截断恢复，也不能在更深层再次软拆。"""
    root_values = [f"User recorded root item {index:02d}" for index in range(20)]
    residual_values = [f"User recorded residual item {index:02d}" for index in range(20)]
    client = _SequenceClient(
        [
            LLMResponse(_compact_response(root_values), "stop", 10),
            LLMResponse('{"claims":[', "length", 11),
            LLMResponse(_compact_response(residual_values), "stop", 12),
            LLMResponse(_compact_response(["User recorded left tail"]), "stop", 13),
            LLMResponse(_compact_response(["User recorded right half"]), "stop", 14),
        ]
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
        ).extract("first paragraph\n\nsecond paragraph\n\nthird paragraph\n\nfourth paragraph")

    assert len(client.requests) == 5
    assert [event[2] for event in audit.events].count("claim_limit_residual_after_split") == 1
    assert [event[2] for event in audit.events].count("claim_limit_split_applied") == 1


def test_full_schema_exact_twenty_does_not_trigger_soft_split_or_limit_audit() -> None:
    """完整响应没有 20 条上限，恰好 20 条不得误触发 compact 恢复。"""
    values = [f"User recorded legacy item {index:02d}" for index in range(20)]
    client = _SequenceClient([LLMResponse(_full_response(values), "stop", 10)])
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            client,
            ChunkingPolicy(10_000, 0, 3),
            soft_split_enabled=True,
        ).extract("\n".join(values))

    assert len(client.requests) == 1
    assert len(claims) == 20
    assert not [event for event in audit.events if event[2].startswith("claim_limit_")]


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


def test_claim_count_overflow_is_bisected_without_schema_retry() -> None:
    """A dense chunk should split instead of asking the model to rewrite more than 20 claims."""

    def compact_claim(value: str, evidence_quote: str) -> dict[str, object]:
        return {
            "subject": "user",
            "value": value,
            "kind": "preference",
            "confidence": 1.0,
            "notability": "high",
            "evidence_quote": evidence_quote,
            "source_event_indices": [0],
        }

    overflow = json.dumps(
        {
            "claims": [compact_claim(f"overflow-{index}", "User likes tea") for index in range(21)],
            "should_memorize": True,
        }
    )
    tea = json.dumps(
        {
            "claims": [compact_claim("User likes tea", "User likes tea")],
            "should_memorize": True,
        }
    )
    coffee = json.dumps(
        {
            "claims": [compact_claim("User likes coffee", "User likes coffee")],
            "should_memorize": True,
        }
    )
    client = _SequenceClient(
        [
            LLMResponse(overflow, "stop", 10),
            LLMResponse(tea, "stop", 11),
            LLMResponse(coffee, "stop", 12),
        ]
    )
    extractor = LLMExtractor(
        client,
        ChunkingPolicy(1_000, 0, 2),
        schema_retries=2,
    )

    claims = extractor.extract("User likes tea.\n\nUser likes coffee.")

    assert {claim.value for claim in claims} == {"User likes tea", "User likes coffee"}
    assert len(client.requests) == 3
    assert extractor._schema_retry_count == 0
    assert extractor.last_usage_tokens == 33


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
