"""LLM 提取前的结构感知内容分块。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hl_mem.domain.content import parse_content

_CONVERSATION_KEYS = ("messages", "conversation", "turns")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])")


class ContentStructure(StrEnum):
    """可识别的提取内容结构。"""

    TEXT = "text"
    CONVERSATION = "conversation"
    JSONL = "jsonl"


@dataclass(frozen=True)
class ExtractionChunk:
    """一次 LLM 提取请求的内容范围。"""

    index: int
    text: str
    structure: ContentStructure
    start_unit: int
    end_unit: int
    context_prefix: str = ""


@dataclass(frozen=True)
class ChunkingPolicy:
    """提取分块大小、对话重叠与递归恢复限制。"""

    target_chars: int
    overlap_turns: int
    max_split_depth: int

    def __post_init__(self) -> None:
        if self.target_chars < 1:
            raise ValueError("target_chars must be positive")
        if self.overlap_turns < 0:
            raise ValueError("overlap_turns must be non-negative")
        if self.max_split_depth < 0:
            raise ValueError("max_split_depth must be non-negative")


def _conversation_turns(content: dict[str, Any] | str) -> list[dict[str, Any]] | None:
    if not isinstance(content, dict):
        return None
    for key in _CONVERSATION_KEYS:
        value = content.get(key)
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    return None


def _content_text(content: dict[str, Any] | str) -> str:
    if isinstance(content, str):
        return content
    return "\n\n".join(part.to_text() for part in parse_content(content))


def _is_jsonl(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    valid = 0
    for line in lines:
        try:
            if isinstance(json.loads(line), dict):
                valid += 1
        except json.JSONDecodeError:
            continue
    return valid / len(lines) >= 0.8


def detect_content_structure(content: dict[str, Any] | str) -> ContentStructure:
    """检测对话、JSONL 或普通文本结构。"""
    if _conversation_turns(content) is not None:
        return ContentStructure.CONVERSATION
    if _is_jsonl(_content_text(content)):
        return ContentStructure.JSONL
    return ContentStructure.TEXT


def _pack_units(units: list[str], target_chars: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    current_chars = 0
    for index, unit in enumerate(units):
        separator_chars = 1 if index > start else 0
        if index > start and current_chars + separator_chars + len(unit) > target_chars:
            ranges.append((start, index))
            start = index
            current_chars = len(unit)
        else:
            current_chars += separator_chars + len(unit)
    if start < len(units):
        ranges.append((start, len(units)))
    return ranges


def _split_oversized_text_unit(text: str, target_chars: int) -> list[str]:
    if len(text) <= target_chars:
        return [text]
    sentences = [part for part in _SENTENCE_BOUNDARY_RE.split(text) if part]
    if len(sentences) > 1:
        pieces: list[str] = []
        for start, end in _pack_units(sentences, target_chars):
            piece = "".join(sentences[start:end])
            pieces.extend(
                piece[offset : offset + target_chars] for offset in range(0, len(piece), target_chars)
            )
        return pieces
    return [text[offset : offset + target_chars] for offset in range(0, len(text), target_chars)]


def _text_units(text: str, target_chars: int) -> list[str]:
    paragraphs = [
        paragraph
        for paragraph in re.split(r"\n\s*\n|(?=^#{1,6}\s)", text, flags=re.MULTILINE)
        if paragraph
    ]
    if not paragraphs:
        return [text]
    return [
        piece
        for paragraph in paragraphs
        for piece in _split_oversized_text_unit(paragraph, target_chars)
    ]


def _conversation_context(turn: dict[str, Any]) -> str:
    context_fields = ("role", "content", "name", "entities", "canonical_entities")
    context_turn = {key: turn[key] for key in context_fields if key in turn}
    return json.dumps(context_turn, ensure_ascii=False, sort_keys=True)


def split_extraction_content(
    content: dict[str, Any] | str,
    policy: ChunkingPolicy,
) -> list[ExtractionChunk]:
    """按内容结构生成稳定、有序且不持久化的提取分块。"""
    structure = detect_content_structure(content)
    if structure is ContentStructure.CONVERSATION:
        turns = _conversation_turns(content) or []
        units = [json.dumps(turn, ensure_ascii=False, sort_keys=True) for turn in turns]
        separator = "\n"
    else:
        text = _content_text(content)
        if len(text) <= policy.target_chars:
            return [ExtractionChunk(0, text, structure, 0, 1)]
        if structure is ContentStructure.JSONL:
            units = [line for line in text.splitlines() if line.strip()]
            separator = "\n"
        else:
            units = _text_units(text, policy.target_chars)
            separator = "\n\n"

    if not units:
        return [ExtractionChunk(0, "", structure, 0, 0)]
    ranges = _pack_units(units, policy.target_chars)
    chunks: list[ExtractionChunk] = []
    for chunk_index, (start, end) in enumerate(ranges):
        context_prefix = ""
        if structure is ContentStructure.CONVERSATION and start > 0 and policy.overlap_turns:
            context_start = max(0, start - policy.overlap_turns)
            turns = _conversation_turns(content) or []
            context_prefix = "\n".join(
                _conversation_context(turn) for turn in turns[context_start:start]
            )
        chunks.append(
            ExtractionChunk(
                index=chunk_index,
                text=separator.join(units[start:end]),
                structure=structure,
                start_unit=start,
                end_unit=end,
                context_prefix=context_prefix,
            )
        )
    return chunks


def _preferred_split_offset(text: str, structure: ContentStructure) -> int | None:
    if len(text) < 2:
        return None
    midpoint = len(text) // 2
    if structure in {ContentStructure.CONVERSATION, ContentStructure.JSONL}:
        line_boundaries = [match.end() for match in re.finditer(r"\n", text)]
        return (
            min(line_boundaries, key=lambda offset: abs(offset - midpoint))
            if line_boundaries
            else None
        )
    candidates: list[int] = []
    for match in re.finditer(r"\n\s*\n|\n|(?<=[。！？!?；;])", text):
        if 0 < match.end() < len(text):
            candidates.append(match.end())
    return min(candidates, key=lambda offset: abs(offset - midpoint)) if candidates else midpoint


def bisect_extraction_chunk(chunk: ExtractionChunk) -> tuple[ExtractionChunk, ExtractionChunk] | None:
    """在最接近中点的结构边界二分一个提取块。"""
    offset = _preferred_split_offset(chunk.text, chunk.structure)
    if offset is None:
        return None
    unit_span = max(1, chunk.end_unit - chunk.start_unit)
    left_units = max(1, min(unit_span - 1, round(unit_span * offset / len(chunk.text)))) if unit_span > 1 else 0
    middle_unit = chunk.start_unit + left_units
    left = ExtractionChunk(
        index=chunk.index,
        text=chunk.text[:offset],
        structure=chunk.structure,
        start_unit=chunk.start_unit,
        end_unit=middle_unit if unit_span > 1 else chunk.end_unit,
        context_prefix=chunk.context_prefix,
    )
    right = ExtractionChunk(
        index=chunk.index,
        text=chunk.text[offset:],
        structure=chunk.structure,
        start_unit=middle_unit if unit_span > 1 else chunk.start_unit,
        end_unit=chunk.end_unit,
        context_prefix=chunk.context_prefix,
    )
    return left, right
