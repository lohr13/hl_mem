"""Raw-session dense retrieval primitives for the LongMemEval native RAG control."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import numpy.typing as npt


class SessionLike(Protocol):
    session_id: str
    occurred_at: str
    messages: Sequence[Mapping[str, str]]


class CaseLike(Protocol):
    question: str
    question_at: str | None
    sessions: Sequence[SessionLike]


@dataclass(frozen=True)
class RawSessionDocument:
    """One complete original session used as a dense retrieval document."""

    session_id: str
    occurred_at: str
    source_index: int
    message_count: int
    text: str


@dataclass(frozen=True)
class RawSessionHit:
    """One selected session in retrieval order."""

    document: RawSessionDocument
    score: float
    retrieval_rank: int


@dataclass(frozen=True)
class NativeRagRender:
    """Reader prompt and both retrieval and reader session orderings."""

    prompt: str
    retrieval_session_ids: tuple[str, ...]
    reader_session_ids: tuple[str, ...]
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


def _render_session(session: SessionLike) -> tuple[str, int]:
    payload = _session_payload(session.messages)
    rendered_messages = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = f"Session Date: {session.occurred_at}\nSession Content:\n{rendered_messages}"
    return text, len(payload)


def render_raw_session_documents(case: CaseLike) -> tuple[RawSessionDocument, ...]:
    """Render every case session without summaries, overlap, or truncation."""
    documents: list[RawSessionDocument] = []
    for source_index, session in enumerate(case.sessions):
        text, message_count = _render_session(session)
        documents.append(
            RawSessionDocument(
                session_id=session.session_id,
                occurred_at=session.occurred_at,
                source_index=source_index,
                message_count=message_count,
                text=text,
            )
        )
    return tuple(documents)


def select_raw_sessions(
    documents: Sequence[RawSessionDocument],
    document_vectors: npt.NDArray[np.float32],
    query_vector: npt.NDArray[np.float32],
    *,
    top_k: int,
) -> tuple[RawSessionHit, ...]:
    """Return exact-cosine Top-K sessions with source order as the stable tie breaker."""
    if top_k < 1:
        raise ValueError("top_k must be positive")
    vectors = np.asarray(document_vectors, dtype=np.float32)
    query = np.asarray(query_vector, dtype=np.float32)
    if vectors.ndim != 2:
        raise ValueError("document vectors must be a two-dimensional matrix")
    if len(documents) != vectors.shape[0]:
        raise ValueError("document vector count does not match sessions")
    if query.ndim != 1 or vectors.shape[1] != query.shape[0]:
        raise ValueError("embedding dimensions differ")

    query_norm = float(np.linalg.norm(query))
    document_norms = np.linalg.norm(vectors, axis=1)
    scores = np.zeros(len(documents), dtype=np.float32)
    if query_norm:
        np.divide(vectors @ query, document_norms * query_norm, out=scores, where=document_norms != 0.0)
    ordered_indices = sorted(
        range(len(documents)),
        key=lambda index: (-float(scores[index]), documents[index].source_index),
    )[: min(top_k, len(documents))]
    return tuple(
        RawSessionHit(
            document=documents[index],
            score=float(scores[index]),
            retrieval_rank=rank,
        )
        for rank, index in enumerate(ordered_indices, start=1)
    )


def render_native_rag_user_prompt(case: CaseLike, hits: Sequence[RawSessionHit]) -> NativeRagRender:
    """Render selected raw sessions chronologically for the shared benchmark reader."""
    reader_hits = sorted(
        hits,
        key=lambda hit: (hit.document.occurred_at, hit.document.source_index),
    )
    chunks = [f"### Session {index}:\n{hit.document.text}\n" for index, hit in enumerate(reader_hits, start=1)]
    history = "\n".join(chunks)
    prompt = (
        "I will give you the timestamped raw chat sessions selected by dense retrieval. "
        "Answer the question using only these sessions.\n\n"
        f"Retrieved Sessions:\n\n{history}\n\n"
        f"Current Date: {case.question_at or 'unknown'}\n"
        f"Question: {case.question}\n"
        "Answer:"
    )
    return NativeRagRender(
        prompt=prompt,
        retrieval_session_ids=tuple(hit.document.session_id for hit in hits),
        reader_session_ids=tuple(hit.document.session_id for hit in reader_hits),
        message_count=sum(hit.document.message_count for hit in reader_hits),
        context_chars=len(history),
        prompt_chars=len(prompt),
    )
