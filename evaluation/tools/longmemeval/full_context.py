"""Full-history rendering and accounting primitives for LongMemEval controls."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class SessionLike(Protocol):
    session_id: str
    occurred_at: str
    messages: Sequence[Mapping[str, str]]


class CaseLike(Protocol):
    question: str
    question_at: str | None
    sessions: Sequence[SessionLike]


@dataclass(frozen=True)
class FullContextRender:
    """One complete reader prompt plus selection diagnostics."""

    prompt: str
    selected_session_ids: tuple[str, ...]
    session_count: int
    message_count: int
    context_chars: int
    prompt_chars: int


def _session_payload(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "role": str(message.get("role") or "user"),
            "content": str(message.get("content") or ""),
        }
        for message in messages
    ]


def render_full_context_user_prompt(case: CaseLike) -> FullContextRender:
    """Render every original session in stable timestamp order without truncation."""
    ordered = sorted(
        enumerate(case.sessions),
        key=lambda item: (item[1].occurred_at, item[0]),
    )
    chunks: list[str] = []
    selected_session_ids: list[str] = []
    message_count = 0
    for output_index, (_source_index, session) in enumerate(ordered, start=1):
        payload = _session_payload(session.messages)
        message_count += len(payload)
        selected_session_ids.append(session.session_id)
        rendered_messages = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        chunks.append(
            f"### Session {output_index}:\n"
            f"Session Date: {session.occurred_at}\n"
            f"Session Content:\n{rendered_messages}\n"
        )

    history = "\n".join(chunks)
    prompt = (
        "I will give you the complete timestamped chat history between you and a user. "
        "Answer the question from this history.\n\n"
        f"History Chats:\n\n{history}\n\n"
        f"Current Date: {case.question_at or 'unknown'}\n"
        f"Question: {case.question}\n"
        "Answer:"
    )
    return FullContextRender(
        prompt=prompt,
        selected_session_ids=tuple(selected_session_ids),
        session_count=len(ordered),
        message_count=message_count,
        context_chars=len(history),
        prompt_chars=len(prompt),
    )
