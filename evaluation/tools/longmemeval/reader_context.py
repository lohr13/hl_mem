"""Query-aware LongMemEval evidence selection and prompt packing."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Protocol

from hl_mem.application.context_packet import estimate_tokens

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


class ReaderCase(Protocol):
    question_at: str | None
    question_type: str
    question: str


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
                "recorded_from": item.get("recorded_from"),
                "recorded_to": item.get("recorded_to"),
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
            candidates.append((round(weight * len(needle_folded)), needle_folded))
        candidates.extend((round(weight * len(unit)), unit) for unit in reader_match_units(needle))
    for _, token in sorted(candidates, reverse=True):
        index = folded.find(token)
        if index >= 0:
            return index + len(token) // 2
    return 0


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


def _top_non_overlapping_centers(scores: Sequence[float]) -> list[int]:
    ranked = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    if not ranked:
        return []
    meaningful_score = max(1.0, scores[ranked[0]] * 0.15)
    selected: list[int] = []
    for index in ranked:
        if selected and scores[index] < meaningful_score:
            break
        start, end = _window_bounds(index, len(scores))
        if any(
            start < selected_end and selected_start < end
            for selected_start, selected_end in (_window_bounds(center, len(scores)) for center in selected)
        ):
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


def reader_turn_window(
    messages: Sequence[Mapping[str, str]],
    question: str,
    needles: Sequence[tuple[str, float]],
) -> tuple[str, dict[str, Any]]:
    scores = [reader_turn_score(str(message.get("content") or ""), question, needles) for message in messages]
    centers_by_score = _top_non_overlapping_centers(scores)
    primary = centers_by_score[0]
    centers = sorted(centers_by_score)
    matched_limit, adjacent_limit = _window_limits(len(centers))
    focus_needles = list(needles)
    if question.strip():
        focus_needles.append((question, 0.5))
    parts: list[str] = []
    included_turns: list[int] = []
    for center in centers:
        start, end = _window_bounds(center, len(messages))
        window_turns = list(range(start, end))
        included_turns.extend(window_turns)
        for index in [center, *(item for item in window_turns if item != center)]:
            message = messages[index]
            if index == center:
                label = "matched"
                token_limit = matched_limit
                excerpt_needles: Sequence[tuple[str, float]] = focus_needles
            else:
                label = "previous" if index < center else "next"
                token_limit = adjacent_limit
                excerpt_needles = ()
            parts.append(
                f"[{label} turn {index} {message.get('role') or 'user'}]\n"
                + reader_turn_excerpt(
                    str(message.get("content") or ""),
                    token_limit,
                    excerpt_needles,
                )
            )
    content = "\n\n".join(parts)
    return content, {
        "mode": "windowed",
        "matched_turn": primary,
        "matched_turns": centers,
        "included_turns": sorted(set(included_turns)),
        "total_turns": len(messages),
        "match_score": round(scores[primary], 6),
        "match_scores": [{"turn": center, "score": round(scores[center], 6)} for center in centers],
    }


def load_reader_events(
    connection: Any,
    event_ids: Sequence[str],
    *,
    question: str = "",
    event_needles: Mapping[str, Sequence[tuple[str, float]]] | None = None,
    context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> list[dict[str, Any]]:
    """Batch-load ranked evidence events without expanding beyond each linked event."""
    if context_mode not in READER_CONTEXT_MODES:
        raise ValueError(f"unsupported reader context mode: {context_mode!r}")
    if connection is None or not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    rows = connection.execute(
        "SELECT id,content_json,occurred_at,recorded_at,event_type,actor_type,source_uri "
        f"FROM events WHERE id IN ({placeholders})",
        tuple(event_ids),
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    events: list[dict[str, Any]] = []
    for event_id in event_ids:
        row = by_id.get(event_id)
        if row is None:
            continue
        try:
            content = json.loads(row["content_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            content = str(row["content_json"] or "")
        locator = content.get("benchmark_locator") if isinstance(content, Mapping) else None
        session_id = locator.get("session_id") if isinstance(locator, Mapping) else None
        event = {
            "event_id": event_id,
            "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
            "event_type": row["event_type"],
            "actor_type": row["actor_type"],
            "session_id": session_id,
            "source_uri": row["source_uri"],
            "content": event_content_text(content),
        }
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
    ranked_events = load_reader_events(
        connection,
        ordered_evidence_ids(claims),
        question=case.question,
        event_needles=reader_event_needles(claims),
        context_mode=context_mode,
    )
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
