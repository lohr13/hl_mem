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


class _SequenceClient:
    """按顺序返回预设 LLM 响应。"""

    class _Provider:
        """最小 provider 标识。"""

        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        """记录请求并返回下一个预设响应。"""
        self.requests.append(request)
        return self.responses.pop(0)


def test_short_text_uses_single_chunk() -> None:
    """短文本保持单块快速路径。"""
    chunks = split_extraction_content("短输入", ChunkingPolicy(100, 1, 2))

    assert len(chunks) == 1
    assert chunks[0].text == "短输入"
    assert chunks[0].context_prefix == ""
    assert chunks[0].structure is ContentStructure.TEXT


def test_conversation_preserves_turns_and_overlap_is_context_only() -> None:
    """对话分块不拆 turn，重叠 turn 仅作为上下文。"""
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
    assert json.loads(chunks[0].text.splitlines()[0]) == content["messages"][0]
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

    extracted_turns = [
        json.loads(line)
        for chunk in chunks
        for line in chunk.text.splitlines()
        if line.strip()
    ]
    assert extracted_turns == turns


def test_text_prefers_paragraph_boundaries_and_can_be_bisected() -> None:
    """普通文本优先按段落切块，生成块仍可继续二分。"""
    content = "第一段。" * 8 + "\n\n" + "第二段。" * 8
    chunks = split_extraction_content(content, ChunkingPolicy(30, 0, 2))
    split = bisect_extraction_chunk(chunks[0])

    assert len(chunks) >= 2
    assert split is not None
    assert split[0].text + split[1].text == chunks[0].text


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

    claims = extractor.extract("第一段内容。\n\n第二段内容。")

    assert len(client.requests) == 3
    assert len(claims) == 1
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
