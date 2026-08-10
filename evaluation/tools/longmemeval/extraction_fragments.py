"""LongMemEval-only extraction fragments for oversized turn events."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_TEXT_FIELDS = ("content", "text")
_PARAGRAPH_BOUNDARY_RE = re.compile(r"\r?\n[ \t]*\r?\n")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?:(?<=[。！？；])|(?<=[.!?;])(?:[ \t]+|\r?\n|$))")
_LINE_BOUNDARY_RE = re.compile(r"\r?\n")
_WORD_BOUNDARY_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ExtractionFragment:
    """One lossless JSON extraction payload plus non-persisted continuity hints."""

    content: dict[str, Any]
    continuity: dict[str, Any]


@dataclass(frozen=True)
class _TextGroup:
    source: str
    turn_fields: tuple[str, ...]
    envelope_fields: tuple[str, ...]

    @property
    def field_paths(self) -> tuple[str, ...]:
        return tuple(f"messages[0].{field}" for field in self.turn_fields) + self.envelope_fields


def _serialized_length(content: Mapping[str, Any]) -> int:
    return len(json.dumps(content, ensure_ascii=False))


def _preferred_boundary(source: str, start: int, maximum_end: int) -> int:
    """Prefer a reasonably full paragraph/sentence boundary before hard splitting."""
    segment = source[start:maximum_end]
    minimum_offset = max(1, len(segment) // 2)
    for pattern in (
        _PARAGRAPH_BOUNDARY_RE,
        _SENTENCE_BOUNDARY_RE,
        _LINE_BOUNDARY_RE,
        _WORD_BOUNDARY_RE,
    ):
        candidates = [match.end() for match in pattern.finditer(segment) if match.end() >= minimum_offset]
        if candidates:
            return start + candidates[-1]
    return maximum_end


def _continuity(
    *,
    fragment_index: int,
    total_fragments: int,
    previous_turns: Sequence[Mapping[str, Any]],
    overlap_turns: int,
    oversized_envelope: bool,
    source_fields: Sequence[str] = (),
) -> dict[str, Any]:
    previous = list(previous_turns[-overlap_turns:]) if overlap_turns else []
    return {
        "fragment_index": fragment_index,
        "total_fragments": total_fragments,
        "continues_previous_fragment": fragment_index > 0,
        "continues_next_fragment": fragment_index + 1 < total_fragments,
        "context_only_previous_turns": copy.deepcopy(previous),
        "oversized_envelope": oversized_envelope,
        "fragment_source_fields": list(source_fields),
    }


def fragment_turn_content(
    content: Mapping[str, Any],
    *,
    target_chars: int,
    previous_turns: Sequence[Mapping[str, Any]] = (),
    overlap_turns: int = 0,
) -> list[ExtractionFragment]:
    """Split one turn event at semantic boundaries without losing its JSON envelope."""
    if target_chars < 1:
        raise ValueError("target_chars must be positive")
    if overlap_turns < 0:
        raise ValueError("overlap_turns must be non-negative")

    original = copy.deepcopy(dict(content))
    raw_messages = original.get("messages")
    if not isinstance(raw_messages, list) or len(raw_messages) != 1 or not isinstance(raw_messages[0], Mapping):
        oversized = _serialized_length(original) > target_chars
        return [
            ExtractionFragment(
                original,
                _continuity(
                    fragment_index=0,
                    total_fragments=1,
                    previous_turns=previous_turns,
                    overlap_turns=overlap_turns,
                    oversized_envelope=oversized,
                ),
            )
        ]

    original_turn = dict(raw_messages[0])
    if _serialized_length(original) <= target_chars:
        oversized = _serialized_length(original) > target_chars
        return [
            ExtractionFragment(
                original,
                _continuity(
                    fragment_index=0,
                    total_fragments=1,
                    previous_turns=previous_turns,
                    overlap_turns=overlap_turns,
                    oversized_envelope=oversized,
                ),
            )
        ]

    grouped: dict[str, dict[str, list[str]]] = {}
    for scope, payload in (("turn", original_turn), ("envelope", original)):
        for field in _TEXT_FIELDS:
            value = payload.get(field)
            if not isinstance(value, str):
                continue
            group_fields = grouped.setdefault(value, {"turn": [], "envelope": []})
            group_fields[scope].append(field)
    groups = [
        _TextGroup(
            source=source,
            turn_fields=tuple(fields["turn"]),
            envelope_fields=tuple(fields["envelope"]),
        )
        for source, fields in grouped.items()
    ]
    if not groups:
        return [
            ExtractionFragment(
                original,
                _continuity(
                    fragment_index=0,
                    total_fragments=1,
                    previous_turns=previous_turns,
                    overlap_turns=overlap_turns,
                    oversized_envelope=True,
                ),
            )
        ]

    def build(active_group: int | None = None, piece: str = "") -> dict[str, Any]:
        payload = copy.deepcopy(original)
        turn = dict(payload["messages"][0])
        for index, group in enumerate(groups):
            value = piece if index == active_group else ""
            for field in group.turn_fields:
                turn[field] = value
            for field in group.envelope_fields:
                payload[field] = value
        payload["messages"][0] = turn
        return payload

    if _serialized_length(build()) >= target_chars:
        return [
            ExtractionFragment(
                original,
                _continuity(
                    fragment_index=0,
                    total_fragments=1,
                    previous_turns=previous_turns,
                    overlap_turns=overlap_turns,
                    oversized_envelope=True,
                ),
            )
        ]

    payloads: list[tuple[dict[str, Any], tuple[str, ...]]] = []
    for group_index, text_group in enumerate(groups):
        offset = 0
        while offset < len(text_group.source):
            low = offset + 1
            high = len(text_group.source)
            maximum_end = offset
            while low <= high:
                end = (low + high) // 2
                if _serialized_length(build(group_index, text_group.source[offset:end])) <= target_chars:
                    maximum_end = end
                    low = end + 1
                else:
                    high = end - 1
            if maximum_end == offset:
                return [
                    ExtractionFragment(
                        original,
                        _continuity(
                            fragment_index=0,
                            total_fragments=1,
                            previous_turns=previous_turns,
                            overlap_turns=overlap_turns,
                            oversized_envelope=True,
                        ),
                    )
                ]
            end = (
                maximum_end
                if maximum_end == len(text_group.source)
                else _preferred_boundary(text_group.source, offset, maximum_end)
            )
            payloads.append((build(group_index, text_group.source[offset:end]), text_group.field_paths))
            offset = end

    total = len(payloads)
    return [
        ExtractionFragment(
            payload,
            _continuity(
                fragment_index=index,
                total_fragments=total,
                previous_turns=previous_turns,
                overlap_turns=overlap_turns,
                oversized_envelope=False,
                source_fields=source_fields,
            ),
        )
        for index, (payload, source_fields) in enumerate(payloads)
    ]
