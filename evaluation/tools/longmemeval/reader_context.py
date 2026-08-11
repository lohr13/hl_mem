"""Query-aware LongMemEval evidence selection and prompt packing."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Protocol

from hl_mem.application.context_packet import estimate_tokens
from hl_mem.recall.lexicalizer import tokenize_for_fts

QA_CONTEXT_TOKEN_BUDGET = 6000
QA_EVIDENCE_EVENT_TOKEN_LIMIT = 1200
QA_CLAIMS_TOKEN_BUDGET = QA_CONTEXT_TOKEN_BUDGET - QA_EVIDENCE_EVENT_TOKEN_LIMIT
QA_CLAIM_FIELD_TOKEN_LIMIT = 192
READER_CONTEXT_MODES = ("windowed", "head")
DEFAULT_READER_CONTEXT_MODE = "windowed"
QA_EVIDENCE_TURN_RADIUS = 1
QA_MATCHED_TURN_TOKEN_LIMIT = 640
QA_ADJACENT_TURN_TOKEN_LIMIT = 192
QA_EVIDENCE_MAX_WINDOWS = 3
_WINDOW_CONTENT_TOKEN_LIMIT = QA_EVIDENCE_EVENT_TOKEN_LIMIT - 192
_ASSISTANT_FTS_CANDIDATE_LIMIT = 32
_ASSISTANT_EXCERPT_TOKEN_LIMIT = QA_EVIDENCE_EVENT_TOKEN_LIMIT - 192
_ASSISTANT_ARTIFACT_RE = re.compile(
    r"\b(?:list|table|script|outline|steps?|items?|options?|recommendations?)\b"
    r"|(?:列表|表格|脚本|清单|第\s*[一二三四五六七八九十\d]+\s*(?:项|条|个))",
    re.IGNORECASE,
)
_ASSISTANT_PROVENANCE_RE = re.compile(
    r"\b(?:you|your)\b.{0,48}\b(?:provided|gave|shared|created|wrote|suggested|mentioned)\b"
    r"|\b(?:previously|earlier|before|above)\b"
    r"|(?:你|您).{0,24}(?:提供|给出|分享|创建|写|建议|提到)"
    r"|(?:之前|先前|前面|上次)",
    re.IGNORECASE,
)
_ENGLISH_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}


class ReaderCase(Protocol):
    question_at: str | None
    question_type: str
    question: str


def assistant_raw_fallback_requested(case: ReaderCase) -> bool:
    """Return whether the narrow assistant-turn retrieval road is warranted."""
    if "assistant" in str(case.question_type or "").casefold():
        return True
    question = str(case.question or "")
    return bool(_ASSISTANT_ARTIFACT_RE.search(question) and _ASSISTANT_PROVENANCE_RE.search(question))


def _or_fts_query(text: str) -> str:
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokenize_for_fts(text))


def _question_ordinal(question: str) -> int | None:
    numeric = re.search(r"\b(\d{1,3})(?:st|nd|rd|th)\b", question, re.IGNORECASE)
    if numeric:
        return int(numeric.group(1))
    folded = question.casefold()
    for word, number in _ENGLISH_ORDINALS.items():
        if re.search(rf"\b{word}\b", folded):
            return number
    chinese = re.search(r"第\s*(\d{1,3})\s*(?:项|条|个)?", question)
    return int(chinese.group(1)) if chinese else None


def _ordinal_item_needle(question: str, content: str) -> str | None:
    ordinal = _question_ordinal(question)
    if ordinal is None:
        return None
    item = re.search(rf"(?m)^\s*{ordinal}\s*[.)、:：-]\s*([^\r\n]+)", content)
    return item.group(0).strip() if item else None


def load_assistant_raw_fallback(connection: Any, case: ReaderCase) -> dict[str, Any] | None:
    """Retrieve one namespace-scoped assistant turn with query-term OR semantics."""
    if connection is None or not assistant_raw_fallback_requested(case):
        return None
    namespace = str(getattr(case, "namespace", "") or "")
    match_query = _or_fts_query(str(case.question or ""))
    if not namespace or not match_query:
        return None
    try:
        rows = connection.execute(
            "SELECT e.id,e.content_json,e.occurred_at,e.recorded_at,e.event_type,"
            "e.actor_type,e.source_uri,e.session_id,bm25(events_fts_v2) AS fts_score "
            "FROM events_fts_v2 JOIN events e ON e.rowid=events_fts_v2.rowid "
            "WHERE events_fts_v2 MATCH ? AND e.tenant_id=? AND e.actor_type='assistant' "
            "ORDER BY fts_score,e.occurred_at DESC,e.id LIMIT ?",
            (match_query, namespace, _ASSISTANT_FTS_CANDIDATE_LIMIT),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None

    # The highest-ranked turn identifies the Top-1 session; it is also the
    # highest-ranked assistant turn within that session under the same order.
    row = rows[0]
    try:
        content = json.loads(row["content_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        content = str(row["content_json"] or "")
    text = event_content_text(content)
    ordinal_needle = _ordinal_item_needle(case.question, text)
    needles: list[tuple[str, float]] = []
    if ordinal_needle:
        needles.append((ordinal_needle, 4.0))
    needles.append((case.question, 1.0))
    excerpt = reader_turn_excerpt(text, _ASSISTANT_EXCERPT_TOKEN_LIMIT, needles)
    locator = content.get("benchmark_locator") if isinstance(content, Mapping) else None
    located_session = locator.get("session_id") if isinstance(locator, Mapping) else None
    return {
        "event_id": str(row["id"]),
        "occurred_at": row["occurred_at"],
        "event_type": row["event_type"],
        "actor_type": row["actor_type"],
        "session_id": located_session or row["session_id"],
        "source_uri": row["source_uri"],
        "retrieval_source": "assistant_raw_fallback",
        "content": excerpt,
        "window": {
            "mode": "assistant_raw_fts",
            "candidate_count": len(rows),
            "fts_score": row["fts_score"],
        },
    }


def normalize_role(value: object) -> str:
    role = str(value or "user").lower()
    return (
        {"human": "user", "ai": "assistant"}.get(role, role)
        if role in {"user", "assistant", "human", "ai", "system", "tool"}
        else "user"
    )


def normalize_content(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def truncate_reader_text(value: object, token_limit: int) -> str:
    """Render one reader field within a deterministic approximate token cap."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if estimate_tokens(text) <= token_limit:
        return text
    marker = "\n[truncated]"
    char_limit = max(0, token_limit * 2 - len(marker) - 2)
    return f"{text[:char_limit]}{marker}"


def reader_claim_records(retrieved: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in retrieved:
        evidence_ids = [str(event_id) for event_id in item.get("evidence_event_ids") or []]
        records.append(
            {
                "rank": item.get("rank"),
                "claim_id": item.get("claim_id"),
                "claim": truncate_reader_text(item.get("text") or "", QA_CLAIM_FIELD_TOKEN_LIMIT),
                "value": (
                    truncate_reader_text(item.get("value"), QA_CLAIM_FIELD_TOKEN_LIMIT)
                    if item.get("value") is not None
                    else None
                ),
                "status": item.get("status"),
                "valid_from": item.get("valid_from"),
                "valid_to": item.get("valid_to"),
                "occurred_start": item.get("occurred_start"),
                "occurred_end": item.get("occurred_end"),
                "evidence_event_ids": list(dict.fromkeys(evidence_ids)),
            }
        )
    return records


def ordered_evidence_ids(retrieved: Sequence[Mapping[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(event_id)
            for item in retrieved
            for event_id in item.get("evidence_event_ids") or []
            if event_id is not None and str(event_id)
        )
    )


def event_content_text(content: object) -> str:
    if isinstance(content, Mapping) and isinstance(content.get("text"), str):
        return str(content["text"])
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def reader_event_needles(
    retrieved: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[tuple[str, float], ...]]:
    by_event: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for item in retrieved:
        values = ((item.get("value"), 3.0), (item.get("text") or item.get("claim"), 2.0))
        for event_id in item.get("evidence_event_ids") or []:
            key = str(event_id)
            for value, weight in values:
                text = str(value or "").strip()
                if text and (text, weight) not in by_event[key]:
                    by_event[key].append((text, weight))
    return {event_id: tuple(needles) for event_id, needles in by_event.items()}


def reader_match_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def fold_reader_needles(
    needles: Sequence[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Fold only obvious wording duplicates used to focus one session excerpt."""
    folded: list[tuple[str, float]] = []
    normalized: list[str] = []
    for needle, weight in needles:
        text = str(needle or "").strip()
        match_text = reader_match_text(text)
        if not match_text:
            continue
        duplicate_index: int | None = None
        for index, existing in enumerate(normalized):
            shorter, longer = sorted((match_text, existing), key=len)
            containment = shorter in longer and len(shorter) >= max(8, round(len(longer) * 0.9))
            similarity = SequenceMatcher(None, match_text, existing, autojunk=False).ratio()
            if match_text == existing or containment or similarity >= 0.92:
                duplicate_index = index
                break
        if duplicate_index is None:
            folded.append((text, weight))
            normalized.append(match_text)
            continue
        old_text, old_weight = folded[duplicate_index]
        if len(text) > len(old_text):
            normalized[duplicate_index] = match_text
            old_text = text
        folded[duplicate_index] = (old_text, max(old_weight, weight))
    return folded


def reader_match_units(value: object) -> set[str]:
    normalized = reader_match_text(value)
    units = {token for token in normalized.split() if len(token) >= 2 and not re.fullmatch(r"[\u3400-\u9fff]+", token)}
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    if len(cjk) == 1:
        units.add(cjk)
    else:
        units.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return units


def reader_match_score(candidate: object, needle: object) -> float:
    candidate_text = reader_match_text(candidate)
    needle_text = reader_match_text(needle)
    if not candidate_text or not needle_text:
        return 0.0
    substring_score = 3.0 if needle_text in candidate_text else 0.0
    if not substring_score and len(candidate_text) >= 6 and candidate_text in needle_text:
        substring_score = 1.5
    candidate_units = reader_match_units(candidate_text)
    needle_units = reader_match_units(needle_text)
    coverage = len(candidate_units & needle_units) / len(needle_units) if needle_units else 0.0
    similarity = SequenceMatcher(None, candidate_text, needle_text, autojunk=False).ratio()
    return substring_score + 2.0 * coverage + similarity


def reader_turn_score(
    content: str,
    question: str,
    needles: Sequence[tuple[str, float]],
) -> float:
    claim_score = max((weight * reader_match_score(content, needle) for needle, weight in needles), default=0.0)
    return claim_score + 0.5 * reader_match_score(content, question)


def reader_focus_index(content: str, needles: Sequence[tuple[str, float]]) -> int:
    folded = unicodedata.normalize("NFKC", content).casefold()
    candidates: list[tuple[int, str]] = []
    for needle, weight in needles:
        needle_folded = unicodedata.normalize("NFKC", needle).casefold().strip()
        if needle_folded:
            index = folded.find(needle_folded)
            if index >= 0:
                return index + len(needle_folded) // 2
            candidates.append((round(weight * len(needle_folded)), needle_folded))
        candidates.extend((round(weight * len(unit)), unit) for unit in reader_match_units(needle))
    for _, token in sorted(candidates, reverse=True):
        index = folded.find(token)
        if index >= 0:
            return index + len(token) // 2
    return 0


def _sentence_aligned_excerpt_start(content: str, start: int, focus: int, char_limit: int) -> int:
    if start <= 0:
        return 0
    boundaries = list(re.finditer(r"[.!?。！？\n]+\s*", content[:start]))
    sentence_start = boundaries[-1].end() if boundaries else 0
    return sentence_start if focus < sentence_start + char_limit else start


def reader_turn_excerpt(
    content: str,
    token_limit: int,
    needles: Sequence[tuple[str, float]] = (),
) -> str:
    if estimate_tokens(content) <= token_limit:
        return content
    leading_marker = "[earlier text omitted]\n"
    trailing_marker = "\n[later text omitted]"
    char_limit = max(1, token_limit * 2 - len(leading_marker) - len(trailing_marker) - 4)
    focus = reader_focus_index(content, needles)
    start = max(0, min(focus - char_limit // 2, len(content) - char_limit))
    start = _sentence_aligned_excerpt_start(content, start, focus, char_limit)
    end = min(len(content), start + char_limit)
    return (leading_marker if start else "") + content[start:end] + (trailing_marker if end < len(content) else "")


def reader_messages(content: object) -> list[dict[str, str]]:
    if not isinstance(content, Mapping):
        return []
    raw_messages = content.get("messages")
    if isinstance(raw_messages, (str, bytes)) or not isinstance(raw_messages, Sequence):
        return []
    messages: list[dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, Mapping):
            continue
        messages.append(
            {
                "role": normalize_role(item.get("role") or item.get("speaker")),
                "content": normalize_content(item.get("content") or item.get("text") or ""),
            }
        )
    return messages


def _window_bounds(center: int, message_count: int) -> tuple[int, int]:
    return (
        max(0, center - QA_EVIDENCE_TURN_RADIUS),
        min(message_count, center + QA_EVIDENCE_TURN_RADIUS + 1),
    )


def _window_positions(
    center: int,
    message_count: int,
    turn_indices: Sequence[int] | None,
) -> list[int]:
    if turn_indices is None:
        start, end = _window_bounds(center, message_count)
        return list(range(start, end))
    center_turn = turn_indices[center]
    return [
        index
        for index, turn_index in enumerate(turn_indices)
        if abs(turn_index - center_turn) <= QA_EVIDENCE_TURN_RADIUS
    ]


def _top_non_overlapping_centers(
    scores: Sequence[float],
    *,
    turn_indices: Sequence[int] | None = None,
) -> list[int]:
    ranked = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    if not ranked:
        return []
    meaningful_score = max(1.0, scores[ranked[0]] * 0.15)
    selected: list[int] = []
    for index in ranked:
        if selected and scores[index] < meaningful_score:
            break
        window = set(_window_positions(index, len(scores), turn_indices))
        if any(window.intersection(_window_positions(center, len(scores), turn_indices)) for center in selected):
            continue
        selected.append(index)
        if len(selected) == QA_EVIDENCE_MAX_WINDOWS:
            break
    return selected or ranked[:1]


def _window_limits(window_count: int) -> tuple[int, int]:
    per_window = max(1, _WINDOW_CONTENT_TOKEN_LIMIT // window_count)
    adjacent = min(QA_ADJACENT_TURN_TOKEN_LIMIT, max(32, per_window // 5))
    matched = min(QA_MATCHED_TURN_TOKEN_LIMIT, max(64, per_window - 2 * adjacent - 48))
    return matched, adjacent


def _prefer_question_center(
    messages: Sequence[Mapping[str, str]],
    question: str,
    centers: Sequence[int],
    turn_indices: Sequence[int] | None,
) -> list[int]:
    """Promote a strong question match already covered only as an adjacent turn."""
    if not question.strip() or not centers:
        return list(centers)
    question_scores = [reader_match_score(str(message.get("content") or ""), question) for message in messages]
    question_center = max(range(len(question_scores)), key=lambda index: (question_scores[index], -index))
    if question_scores[question_center] < 1.25 or question_center in centers:
        return list(centers)
    promoted = list(centers)
    for position, center in enumerate(promoted):
        if question_center in _window_positions(center, len(messages), turn_indices):
            promoted[position] = question_center
            return promoted
    return promoted


def reader_turn_window(
    messages: Sequence[Mapping[str, str]],
    question: str,
    needles: Sequence[tuple[str, float]],
    *,
    turn_indices: Sequence[int] | None = None,
) -> tuple[str, dict[str, Any]]:
    if turn_indices is not None and len(turn_indices) != len(messages):
        raise ValueError("turn_indices must align with messages")
    displayed_turns = list(turn_indices) if turn_indices is not None else list(range(len(messages)))
    scores = [reader_turn_score(str(message.get("content") or ""), question, needles) for message in messages]
    centers_by_score = _prefer_question_center(
        messages,
        question,
        _top_non_overlapping_centers(scores, turn_indices=turn_indices),
        turn_indices,
    )
    primary = centers_by_score[0]
    centers = sorted(centers_by_score)
    matched_limit, adjacent_limit = _window_limits(len(centers))
    folded_needles = fold_reader_needles(needles)
    parts: list[str] = []
    included_turns: list[int] = []
    for center in centers:
        window_turns = _window_positions(center, len(messages), turn_indices)
        included_turns.extend(window_turns)
        for index in [center, *(item for item in window_turns if item != center)]:
            message = messages[index]
            displayed_turn = displayed_turns[index]
            if index == center:
                label = "matched"
                token_limit = matched_limit
                if question.strip() and normalize_role(message.get("role")) == "user":
                    excerpt_needles = [(question, 0.5), *folded_needles]
                else:
                    excerpt_needles = [*folded_needles, (question, 0.5)] if question.strip() else folded_needles
            else:
                label = "previous" if index < center else "next"
                token_limit = adjacent_limit
                excerpt_needles = ()
            parts.append(
                f"[{label} turn {displayed_turn} {message.get('role') or 'user'}]\n"
                + reader_turn_excerpt(
                    str(message.get("content") or ""),
                    token_limit,
                    excerpt_needles,
                )
            )
    content = "\n\n".join(parts)
    return content, {
        "mode": "windowed",
        "matched_turn": displayed_turns[primary],
        "matched_turns": [displayed_turns[index] for index in centers],
        "included_turns": sorted({displayed_turns[index] for index in included_turns}),
        "total_turns": len(messages),
        "match_score": round(scores[primary], 6),
        "match_scores": [{"turn": displayed_turns[center], "score": round(scores[center], 6)} for center in centers],
    }


def _load_ranked_event_rows(connection: Any, event_ids: Sequence[str]) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in event_ids)
    try:
        rows = connection.execute(
            "SELECT id,tenant_id,session_id AS stored_session_id,content_json,occurred_at,recorded_at,"
            "event_type,actor_type,source_uri "
            f"FROM events WHERE id IN ({placeholders})",
            tuple(event_ids),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = connection.execute(
            "SELECT id,NULL AS tenant_id,NULL AS stored_session_id,content_json,occurred_at,recorded_at,"
            "event_type,actor_type,source_uri "
            f"FROM events WHERE id IN ({placeholders})",
            tuple(event_ids),
        ).fetchall()
    return {str(row["id"]): row for row in rows}


def _decoded_event_content(row: Any) -> object:
    try:
        return json.loads(row["content_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(row["content_json"] or "")


def _benchmark_locator(content: object) -> Mapping[str, Any] | None:
    locator = content.get("benchmark_locator") if isinstance(content, Mapping) else None
    return locator if isinstance(locator, Mapping) else None


def _event_session_key(row: Any, content: object) -> tuple[str, str] | None:
    locator = _benchmark_locator(content)
    locator_session = locator.get("session_id") if locator is not None else None
    session_id = str(locator_session or row["stored_session_id"] or "").strip()
    tenant_id = str(row["tenant_id"] or "").strip()
    turn_index = locator.get("turn_index") if locator is not None else None
    if not tenant_id or not session_id or not isinstance(turn_index, int) or isinstance(turn_index, bool):
        return None
    return tenant_id, session_id


def _event_record(row: Any, content: object, *, event_id: str | None = None) -> dict[str, Any]:
    locator = _benchmark_locator(content)
    located_session = locator.get("session_id") if locator is not None else None
    return {
        "event_id": event_id or str(row["id"]),
        "occurred_at": row["occurred_at"],
        "event_type": row["event_type"],
        "actor_type": row["actor_type"],
        "session_id": located_session or row["stored_session_id"],
        "source_uri": row["source_uri"],
        "content": event_content_text(content),
    }


def _load_session_turns(
    connection: Any,
    key: tuple[str, str],
) -> tuple[list[dict[str, str]], list[int], dict[int, str]] | None:
    tenant_id, session_id = key
    rows = connection.execute(
        "SELECT id,content_json,actor_type FROM events WHERE tenant_id=? AND session_id=?",
        (tenant_id, session_id),
    ).fetchall()
    turns: list[tuple[int, str, dict[str, str]]] = []
    for row in rows:
        content = _decoded_event_content(row)
        locator = _benchmark_locator(content)
        turn_index = locator.get("turn_index") if locator is not None else None
        if not isinstance(turn_index, int) or isinstance(turn_index, bool):
            return None
        messages = reader_messages(content)
        if len(messages) != 1:
            return None
        turns.append((turn_index, str(row["id"]), messages[0]))
    if not turns:
        return None
    turns.sort(key=lambda item: (item[0], item[1]))
    if len({turn_index for turn_index, _, _ in turns}) != len(turns):
        return None
    return (
        [message for _, _, message in turns],
        [turn_index for turn_index, _, _ in turns],
        {turn_index: event_id for turn_index, event_id, _ in turns},
    )


def load_reader_events(
    connection: Any,
    event_ids: Sequence[str],
    *,
    question: str = "",
    event_needles: Mapping[str, Sequence[tuple[str, float]]] | None = None,
    context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> list[dict[str, Any]]:
    """Batch-load ranked evidence, rebuilding turn-event sessions for windowing."""
    if context_mode not in READER_CONTEXT_MODES:
        raise ValueError(f"unsupported reader context mode: {context_mode!r}")
    if connection is None or not event_ids:
        return []
    by_id = _load_ranked_event_rows(connection, event_ids)
    decoded = {event_id: _decoded_event_content(row) for event_id, row in by_id.items()}
    grouped_event_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    if context_mode == "windowed":
        for event_id in event_ids:
            row = by_id.get(event_id)
            if row is None:
                continue
            key = _event_session_key(row, decoded[event_id])
            if key is not None:
                grouped_event_ids[key].append(event_id)
    events: list[dict[str, Any]] = []
    emitted_sessions: set[tuple[str, str]] = set()
    for event_id in event_ids:
        row = by_id.get(event_id)
        if row is None:
            continue
        content = decoded[event_id]
        key = _event_session_key(row, content) if context_mode == "windowed" else None
        if key is not None and key not in emitted_sessions:
            session_turns = _load_session_turns(connection, key)
            if session_turns is not None:
                messages, turn_indices, event_id_by_turn = session_turns
                linked_ids = grouped_event_ids[key]
                needles = fold_reader_needles(
                    [needle for linked_id in linked_ids for needle in tuple((event_needles or {}).get(linked_id, ()))]
                )
                window_content, window = reader_turn_window(
                    messages,
                    question,
                    needles,
                    turn_indices=turn_indices,
                )
                window["included_event_ids"] = [
                    event_id_by_turn[turn_index]
                    for turn_index in window["included_turns"]
                    if turn_index in event_id_by_turn
                ]
                event = _event_record(row, content, event_id=linked_ids[0])
                event["evidence_event_ids"] = linked_ids
                event["content"] = window_content
                event["window"] = window
                events.append(event)
                emitted_sessions.add(key)
                continue
        if key is not None and key in emitted_sessions:
            continue
        event = _event_record(row, content)
        messages = reader_messages(content)
        if context_mode == "windowed" and messages:
            window_content, window = reader_turn_window(
                messages,
                question,
                tuple((event_needles or {}).get(event_id, ())),
            )
            event["content"] = window_content
            event["window"] = window
        events.append(event)
    return events


def render_reader_user_prompt(
    case: ReaderCase,
    claims: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> str:
    current_date = case.question_at or datetime.now(timezone.utc).isoformat()
    claims_json = json.dumps(claims, ensure_ascii=False, separators=(",", ":"))
    events_json = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    return (
        f"Current Date: {current_date}\n"
        f"Question Type: {case.question_type}\n\n"
        f"Reader Context Mode: {context_mode}\n\n"
        f"Memory Claims:\n{claims_json or '[]'}\n\n"
        f"Original Evidence Events:\n{events_json or '[]'}\n\n"
        f"Question: {case.question}"
    )


def fit_reader_claim(
    case: ReaderCase,
    accepted_claims: Sequence[Mapping[str, Any]],
    claim: Mapping[str, Any],
    context_mode: str,
) -> dict[str, Any] | None:
    evidence_ids = [str(event_id) for event_id in claim.get("evidence_event_ids") or []]
    low = 0
    high = len(evidence_ids)
    best: dict[str, Any] | None = None
    while low <= high:
        count = (low + high) // 2
        candidate = {**claim, "evidence_event_ids": evidence_ids[:count]}
        omitted = len(evidence_ids) - count
        if omitted:
            candidate["evidence_event_ids_omitted"] = omitted
        prompt = render_reader_user_prompt(case, [*accepted_claims, candidate], [], context_mode)
        if estimate_tokens(prompt) <= QA_CLAIMS_TOKEN_BUDGET:
            best = candidate
            low = count + 1
        else:
            high = count - 1
    return best


def fit_reader_claims(
    case: ReaderCase,
    claims: Sequence[Mapping[str, Any]],
    context_mode: str,
) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for claim in claims:
        fitted = fit_reader_claim(case, accepted, claim, context_mode)
        if fitted is None:
            break
        accepted.append(fitted)
    return accepted


def fit_reader_event(
    case: ReaderCase,
    claims: Sequence[Mapping[str, Any]],
    accepted_events: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
    context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> dict[str, Any] | None:
    original = str(event.get("content") or "")
    max_chars = min(len(original), QA_EVIDENCE_EVENT_TOKEN_LIMIT * 2)
    low = 0
    high = max_chars
    best: dict[str, Any] | None = None
    while low <= high:
        length = (low + high) // 2
        truncated = length < len(original)
        content = original[:length] + ("\n[truncated]" if truncated else "")
        candidate = {**event, "content": content}
        serialized_event = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        prompt = render_reader_user_prompt(case, claims, [*accepted_events, candidate], context_mode)
        if (
            estimate_tokens(serialized_event) <= QA_EVIDENCE_EVENT_TOKEN_LIMIT
            and estimate_tokens(prompt) <= QA_CONTEXT_TOKEN_BUDGET
        ):
            best = candidate
            low = length + 1
        else:
            high = length - 1
    return best


def build_reader_user_prompt(
    connection: Any,
    case: ReaderCase,
    retrieved: Sequence[Mapping[str, Any]],
    context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> str:
    """Build time/source-aware reader context under a strict total budget."""
    if context_mode not in READER_CONTEXT_MODES:
        raise ValueError(f"unsupported reader context mode: {context_mode!r}")
    claims = fit_reader_claims(case, reader_claim_records(retrieved), context_mode)
    assistant_event = load_assistant_raw_fallback(connection, case)
    ranked_events = load_reader_events(
        connection,
        ordered_evidence_ids(claims),
        question=case.question,
        event_needles=reader_event_needles(claims),
        context_mode=context_mode,
    )
    if assistant_event is not None:
        ranked_events = [
            assistant_event,
            *(event for event in ranked_events if event.get("event_id") != assistant_event["event_id"]),
        ]
    accepted_events: list[dict[str, Any]] = []
    for event in ranked_events:
        fitted = fit_reader_event(case, claims, accepted_events, event, context_mode)
        if fitted is None:
            break
        accepted_events.append(fitted)
    prompt = render_reader_user_prompt(case, claims, accepted_events, context_mode)
    if estimate_tokens(prompt) > QA_CONTEXT_TOKEN_BUDGET:
        raise RuntimeError("reader context budget invariant violated")
    return prompt
