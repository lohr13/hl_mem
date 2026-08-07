#!/usr/bin/env python
"""Run LongMemEval-S against hl_mem's production extraction and recall stack."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools.run_embedding_ablation import (  # noqa: E402
    Cost,
    DashScopeEmbeddingClient,
    EmbeddingConfig,
    embed_remote,
)
from hl_mem import __version__  # noqa: E402
from hl_mem.application.ingest import IngestService  # noqa: E402
from hl_mem.application.recall import RecallService  # noqa: E402
from hl_mem.components import (  # noqa: E402
    initialize_process,
    make_embedder,
    make_extractor,
    make_llm_client,
    make_query_expander,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.core.vector import cosine_similarity, pack_vector  # noqa: E402
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION  # noqa: E402
from hl_mem.llm.types import (  # noqa: E402
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.recall.relation_expansion import RelationExpansionConfig  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402

DEFAULT_DATASET = ROOT / "evaluation" / "longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "longmemeval_s_benchmark.json"
DATABASE_ROOT = ROOT / "var" / "benchmark_lme"
COMPARE_ROOT = DATABASE_ROOT / "config_compare"
COMPARE_CACHE = ROOT / "evaluation" / "cache" / "longmemeval_config_compare"
LME_12_BACKUP_ROOT = ROOT / "evaluation" / "cache" / "lme_12_backup"
THRESHOLD_ANALYSIS_OUTPUT = ROOT / "evaluation" / "results" / "lme_12_threshold_analysis.json"
DEFAULT_CONFIG = ROOT / "hl_mem.toml"
DEFAULT_ENV_FILE = ROOT / ".env"
QA_MODEL = "qwen3.7-plus"
RETRIEVAL_KS = (1, 5, 10)
JSON_READ_CHARS = 1024 * 1024
FALLBACK_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
CLAIM_RELEVANCE_THRESHOLD = 0.5
SIMILARITY_THRESHOLDS = (0.2, 0.3, 0.4, 0.5, 0.65)
RELEVANCE_SCORER_CODE = "V0"
RELEVANCE_LABEL_VERSION = "claim-answer-cosine-v2"
QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "knowledge-update",
    "temporal-reasoning",
)

EMBEDDING_CONFIGS: dict[str, dict[str, str | None]] = {
    "V0": {
        "model": "text-embedding-v4",
        "api": "compatible",
        "text_type": None,
        "output_type": "dense",
    },
    "Q0": {
        "model": "qwen3.7-text-embedding",
        "api": "compatible",
        "text_type": None,
        "output_type": "dense",
    },
    "Q1": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": None,
        "output_type": "dense",
    },
    "Q2": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": "query/document",
        "output_type": "dense",
    },
    "Q3": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": None,
        "output_type": "instruct",
    },
    "Q4": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": "query/document",
        "output_type": "sparse",
    },
}


@dataclass(frozen=True)
class SessionInput:
    """One LongMemEval session represented as one hl_mem event."""

    session_id: str
    event_id: str
    occurred_at: str
    messages: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class LongMemEvalCase:
    """Normalized benchmark case independent of the source JSON variant."""

    case_id: str
    question_type: str
    question: str
    answer: str
    question_at: str | None
    sessions: tuple[SessionInput, ...]
    gold_event_ids: tuple[str, ...]
    gold_session_ids: tuple[str, ...]

    @property
    def namespace(self) -> str:
        return f"eval:longmemeval:{self.case_id}"


def _embedding_config(code: str, definition: Mapping[str, str | None]) -> EmbeddingConfig:
    output_type = definition["output_type"]
    return EmbeddingConfig(
        code=code,
        model=str(definition["model"]),
        api_kind=str(definition["api"]),
        dim=2048,
        batch_size=20 if code == "Q0" else 10,
        use_text_type=definition["text_type"] == "query/document",
        use_instruct=output_type == "instruct",
        use_sparse=output_type == "sparse",
    )


def _relevance_scorer_metadata() -> dict[str, Any]:
    """Return the fixed, candidate-independent scorer used to label claims."""
    definition = EMBEDDING_CONFIGS[RELEVANCE_SCORER_CODE]
    config = _embedding_config(RELEVANCE_SCORER_CODE, definition)
    return {
        "code": RELEVANCE_SCORER_CODE,
        "model": config.model,
        "api": config.api_kind,
        "text_type": definition["text_type"],
        "output_type": definition["output_type"],
        "dimension": config.dim,
        "label_version": RELEVANCE_LABEL_VERSION,
        "threshold": CLAIM_RELEVANCE_THRESHOLD,
    }


class ConfigCompareEmbedder:
    """Adapter around the ablation client for hl_mem's embedder protocol."""

    def __init__(
        self,
        client: DashScopeEmbeddingClient,
        config: EmbeddingConfig,
        cache_dir: Path,
    ) -> None:
        self.client = client
        self.config = config
        self.cache_dir = cache_dir
        self.model = config.model
        self.dim = config.dim
        self.sparse_requested = config.use_sparse
        self.sparse_rows_received = 0
        self.cost = Cost()

    def _embed(self, role: str, texts: list[str]) -> list[bytes]:
        output = embed_remote(
            self.client,
            self.config,
            role,
            texts,
            cache_dir=self.cache_dir,
            use_cache=True,
        )
        self.cost.add(output.cost)
        if output.sparse is not None:
            self.sparse_rows_received += len(output.sparse)
        return [pack_vector(row) for row in output.dense]

    def embed_documents(self, texts: list[str]) -> list[bytes]:
        return self._embed("document", texts)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return self.embed_documents(texts)

    def embed_one(self, text: str) -> bytes:
        return self.embed_documents([text])[0]

    def embed_query(self, text: str) -> bytes:
        return self._embed("query", [text])[0]

    def embed_query_batch(self, texts: list[str]) -> list[bytes]:
        return self._embed("query", texts)

    def cost_snapshot(self) -> dict[str, int | float]:
        return self.cost.as_dict()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--no-qa", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--config-compare",
        action="store_true",
        help="extract once, then compare V0/Q0/Q1/Q2/Q3/Q4 on a stratified sample",
    )
    return parser.parse_args(argv)


def iter_case_records(path: Path, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Stream a top-level JSON array and stop without reading its unused tail."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer")
    if not path.is_file():
        raise FileNotFoundError(f"LongMemEval dataset does not exist: {path}")

    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8-sig") as stream:
        buffer = ""
        position = 0
        eof = False

        def read_more() -> bool:
            nonlocal buffer, position, eof
            chunk = stream.read(JSON_READ_CHARS)
            buffer = buffer[position:] + chunk
            position = 0
            eof = not bool(chunk)
            return bool(chunk)

        read_more()
        while True:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if position < len(buffer):
                break
            if not read_more():
                raise ValueError(f"LongMemEval dataset is empty or incomplete: {path}")
        if buffer[position] != "[":
            raise ValueError("LongMemEval dataset must be a top-level JSON array")
        position += 1

        yielded = 0
        while True:
            while True:
                while position < len(buffer) and (buffer[position].isspace() or buffer[position] == ","):
                    position += 1
                if position < len(buffer):
                    break
                if not read_more():
                    raise ValueError(f"LongMemEval dataset is incomplete: {path}")

            if buffer[position] == "]":
                return
            try:
                record, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError as error:
                if read_more():
                    continue
                raise ValueError(f"LongMemEval dataset is incomplete or invalid near EOF: {path}") from error
            if not isinstance(record, dict):
                raise ValueError("every LongMemEval case must be a JSON object")
            yield record
            yielded += 1
            position = end
            if limit is not None and yielded >= limit:
                return


def _sample_stratified(
    cases: Sequence[Mapping[str, Any]],
    n_per_type: int = 2,
) -> list[Mapping[str, Any]]:
    """Take the first N cases of each official question type, deterministically."""
    if n_per_type < 1:
        raise ValueError("n_per_type must be positive")
    by_type: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        question_type = str(case.get("question_type") or case.get("type") or "uncategorized")
        if question_type in QUESTION_TYPES and len(by_type[question_type]) < n_per_type:
            by_type[question_type].append(case)
    missing = {
        question_type: n_per_type - len(by_type[question_type])
        for question_type in QUESTION_TYPES
        if len(by_type[question_type]) < n_per_type
    }
    if missing:
        raise ValueError(f"dataset lacks enough cases for stratified sampling: {missing}")
    return [case for question_type in QUESTION_TYPES for case in by_type[question_type]]


def _sequence(value: object, field: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a list")
    return list(value)


def _normalize_role(value: object) -> str:
    role = str(value or "user").lower()
    return (
        {"human": "user", "ai": "assistant"}.get(role, role)
        if role
        in {
            "user",
            "assistant",
            "human",
            "ai",
            "system",
            "tool",
        }
        else "user"
    )


def _normalize_content(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _timestamp(value: object, fallback_index: int | None = None) -> str:
    text = str(value or "").strip()
    if text:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is None:
            for pattern in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise ValueError(f"unsupported LongMemEval timestamp: {text!r}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return (FALLBACK_EPOCH + timedelta(seconds=fallback_index or 0)).isoformat()


def _event_id(case_id: str, session_id: str, index: int) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    return f"lme:{case_id}:session:{index:03d}:{digest}"


def _evidence_tokens(value: object) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, (str, int)) else _sequence(value, "evidence")
    result: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            item = item.get("session_id") or item.get("dialog_id") or item.get("message_id") or item.get("id")
        if item is not None and str(item):
            result.append(str(item))
    return list(dict.fromkeys(result))


def _session_messages(raw_session: object) -> list[Mapping[str, Any]]:
    if isinstance(raw_session, Mapping):
        raw_session = raw_session.get("messages") or raw_session.get("turns") or []
    return [item for item in _sequence(raw_session, "haystack_sessions[]") if isinstance(item, Mapping)]


def _normalize_official_sessions(
    case_id: str,
    record: Mapping[str, Any],
) -> tuple[list[SessionInput], dict[str, list[str]], set[str]]:
    raw_sessions = _sequence(record.get("haystack_sessions"), "haystack_sessions")
    raw_ids = _sequence(record.get("haystack_session_ids"), "haystack_session_ids")
    raw_dates = _sequence(record.get("haystack_dates"), "haystack_dates")
    if len(raw_sessions) != len(raw_ids) or len(raw_sessions) != len(raw_dates):
        raise ValueError(f"case {case_id}: haystack_sessions, haystack_session_ids, and haystack_dates must align")

    sessions: list[SessionInput] = []
    aliases: dict[str, list[str]] = {}
    answer_marked: set[str] = set()
    occurrences: dict[str, int] = {}
    for index, (raw_session, raw_id, raw_date) in enumerate(zip(raw_sessions, raw_ids, raw_dates, strict=True)):
        source_session_id = str(raw_id)
        occurrences[source_session_id] = occurrences.get(source_session_id, 0) + 1
        occurrence = occurrences[source_session_id]
        session_id = source_session_id if occurrence == 1 else f"{source_session_id}#{occurrence}"
        event_id = _event_id(case_id, session_id, index)
        turns = _session_messages(raw_session)
        messages = tuple(
            {
                "role": _normalize_role(turn.get("role") or turn.get("speaker")),
                "content": _normalize_content(turn.get("content") or turn.get("text") or ""),
            }
            for turn in turns
        )
        sessions.append(SessionInput(session_id, event_id, _timestamp(raw_date, index), messages))
        aliases.setdefault(source_session_id, []).append(event_id)
        if session_id != source_session_id:
            aliases.setdefault(session_id, []).append(event_id)
        aliases.setdefault(str(index), []).append(event_id)
        for turn in turns:
            for key in ("id", "dialog_id", "message_id"):
                if turn.get(key) is not None:
                    aliases.setdefault(str(turn[key]), []).append(event_id)
            if bool(turn.get("has_answer")):
                answer_marked.add(source_session_id)
    return sessions, aliases, answer_marked


def _normalize_flat_sessions(
    case_id: str,
    record: Mapping[str, Any],
) -> tuple[list[SessionInput], dict[str, list[str]], set[str]]:
    raw_turns = _sequence(record.get("chat_history") or [], "chat_history")
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, Mapping):
            continue
        session_id = str(raw_turn.get("session_id") or f"session-{index:04d}")
        grouped.setdefault(session_id, []).append(raw_turn)

    sessions: list[SessionInput] = []
    aliases: dict[str, list[str]] = {}
    answer_marked: set[str] = set()
    for index, (session_id, turns) in enumerate(grouped.items()):
        event_id = _event_id(case_id, session_id, index)
        occurred_at = next(
            (
                _timestamp(turn.get("timestamp") or turn.get("date"), index)
                for turn in turns
                if turn.get("timestamp") or turn.get("date")
            ),
            _timestamp(None, index),
        )
        messages = tuple(
            {
                "role": _normalize_role(turn.get("role") or turn.get("speaker")),
                "content": _normalize_content(turn.get("content") or turn.get("text") or ""),
            }
            for turn in turns
        )
        sessions.append(SessionInput(session_id, event_id, occurred_at, messages))
        aliases.setdefault(session_id, []).append(event_id)
        for turn in turns:
            for key in ("id", "dialog_id", "message_id"):
                if turn.get(key) is not None:
                    aliases.setdefault(str(turn[key]), []).append(event_id)
            if bool(turn.get("has_answer")):
                answer_marked.add(session_id)
    return sessions, aliases, answer_marked


def normalize_case(record: Mapping[str, Any]) -> LongMemEvalCase:
    """Normalize official LongMemEval-S and the documented flat fallback."""
    case_id = str(record.get("question_id") or record.get("case_id") or record.get("id") or "").strip()
    if not case_id:
        raise ValueError("LongMemEval case is missing question_id/id")
    question = str(record.get("question") or record.get("query") or "").strip()
    if not question:
        raise ValueError(f"case {case_id}: question is empty")

    if record.get("haystack_sessions") is not None:
        sessions, aliases, answer_marked = _normalize_official_sessions(case_id, record)
        raw_evidence = record.get("answer_session_ids")
    else:
        sessions, aliases, answer_marked = _normalize_flat_sessions(case_id, record)
        raw_evidence = record.get("evidence") or record.get("answer_session_ids")
    if not sessions:
        raise ValueError(f"case {case_id}: chat history contains no sessions")

    evidence_tokens = _evidence_tokens(raw_evidence) or sorted(answer_marked)
    unresolved = [token for token in evidence_tokens if token not in aliases]
    if unresolved:
        raise ValueError(f"case {case_id}: evidence IDs do not map to sessions: {unresolved}")
    gold_event_ids = tuple(dict.fromkeys(event_id for token in evidence_tokens for event_id in aliases[token]))
    event_to_session = {session.event_id: session.session_id for session in sessions}
    gold_session_ids = tuple(event_to_session[event_id] for event_id in gold_event_ids)
    question_date = record.get("question_date") or record.get("as_of")
    return LongMemEvalCase(
        case_id=case_id,
        question_type=str(record.get("question_type") or record.get("type") or "uncategorized"),
        question=question,
        answer=str(record.get("answer") or record.get("gold_answer") or ""),
        question_at=_timestamp(question_date) if question_date else None,
        sessions=tuple(sessions),
        gold_event_ids=gold_event_ids,
        gold_session_ids=gold_session_ids,
    )


def _result_evidence_ids(result: Mapping[str, Any]) -> tuple[str, ...]:
    evidence = result.get("evidence") or []
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        return ()
    ids: list[str] = []
    for item in evidence:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, Mapping):
            value = item.get("event_id") or item.get("evidence_id") or item.get("id")
            if value is not None:
                ids.append(str(value))
    return tuple(dict.fromkeys(ids))


def _claim_relevance_scores(
    claims: Mapping[str, str],
    answer: str,
    embedder: Any,
) -> dict[str, float]:
    """Score claim values against the reference answer with one fixed embedder."""
    if not answer.strip() or not claims:
        return {}
    claim_ids = list(claims)
    blobs = embedder.embed_batch([claims[claim_id] for claim_id in claim_ids] + [answer])
    if len(blobs) != len(claim_ids) + 1:
        raise ValueError("relevance embedder returned an unexpected vector count")
    answer_blob = blobs[-1]
    return {
        claim_id: cosine_similarity(blob, answer_blob) for claim_id, blob in zip(claim_ids, blobs[:-1], strict=True)
    }


def _claim_similarity_records(
    claims: Sequence[Mapping[str, Any]],
    case: LongMemEvalCase,
    embedder: Any,
) -> list[dict[str, Any]]:
    """Score every claim against answer and question and retain evidence provenance."""
    if not claims:
        return []
    values = [str(claim.get("value") or "") for claim in claims]
    document_texts = values + ([case.answer] if case.answer.strip() else [])
    document_blobs = embedder.embed_batch(document_texts)
    if len(document_blobs) != len(document_texts):
        raise ValueError("similarity embedder returned an unexpected vector count")
    answer_blob = document_blobs[-1] if case.answer.strip() else None
    question_blob = embedder.embed_query(case.question) if case.question.strip() else None
    event_to_session = {session.event_id: session.session_id for session in case.sessions}
    gold_events = set(case.gold_event_ids)
    records: list[dict[str, Any]] = []
    for claim, claim_blob in zip(claims, document_blobs[: len(values)], strict=True):
        evidence_event_ids = [str(item) for item in claim.get("evidence_event_ids") or []]
        record = dict(claim)
        record["answer_similarity"] = cosine_similarity(claim_blob, answer_blob) if answer_blob else None
        record["question_similarity"] = cosine_similarity(claim_blob, question_blob) if question_blob else None
        record["evidence_event_ids"] = evidence_event_ids
        record["evidence_session_ids"] = [
            event_to_session[event_id] for event_id in evidence_event_ids if event_id in event_to_session
        ]
        record["from_answer_session"] = bool(set(evidence_event_ids) & gold_events)
        records.append(record)
    return records


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _similarity_distribution(values: Sequence[float]) -> dict[str, Any]:
    scores = [float(value) for value in values]
    return {
        "count": len(scores),
        "max": max(scores) if scores else None,
        "median": median(scores) if scores else None,
        "p75": _percentile(scores, 0.75),
        "p90": _percentile(scores, 0.90),
        "p95": _percentile(scores, 0.95),
        "counts_above_threshold": {
            f">{threshold:g}": sum(score > threshold for score in scores) for threshold in SIMILARITY_THRESHOLDS
        },
    }


def _similarity_breakdown(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    def scores(label: bool | None = None) -> list[float]:
        return [
            float(record[field])
            for record in records
            if record.get(field) is not None and (label is None or record.get("from_answer_session") is label)
        ]

    return {
        "all_claims": _similarity_distribution(scores()),
        "answer_session_claims": _similarity_distribution(scores(True)),
        "other_session_claims": _similarity_distribution(scores(False)),
    }


def _threshold_analysis(
    cases: Sequence[LongMemEvalCase],
    payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    per_case: list[dict[str, Any]] = []
    all_claims: list[dict[str, Any]] = []
    for case in cases:
        claims = [dict(item) for item in payloads[case.case_id].get("claims", [])]
        all_claims.extend(claims)
        per_case.append(
            {
                "case_id": case.case_id,
                "question_type": case.question_type,
                "question": case.question,
                "answer": case.answer,
                "answer_session_ids": list(case.gold_session_ids),
                "claim_count": len(claims),
                "answer_similarity": _similarity_breakdown(claims, "answer_similarity"),
                "question_similarity": _similarity_breakdown(claims, "question_similarity"),
                "claims": claims,
            }
        )
    return {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "mode": "claim_similarity_threshold_analysis",
        "status": "completed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "relevance_scorer": _relevance_scorer_metadata(),
            "similarities": ["cosine(claim_value, answer)", "cosine(claim_value, question)"],
            "percentiles": "linear interpolation over sorted scores",
            "threshold_comparison": "strictly greater than threshold",
            "thresholds": list(SIMILARITY_THRESHOLDS),
            "ground_truth_label": "claim evidence event belongs to an answer_session_id",
        },
        "cases": len(cases),
        "claims": len(all_claims),
        "global": {
            "answer_similarity": _similarity_breakdown(all_claims, "answer_similarity"),
            "question_similarity": _similarity_breakdown(all_claims, "question_similarity"),
        },
        "per_case": per_case,
    }


def _session_retrieval_metrics(
    results: Sequence[Mapping[str, Any]],
    gold_event_ids: Sequence[str],
) -> dict[str, Any]:
    gold = set(gold_event_ids)
    if not gold:
        return {
            "eligible": False,
            **{f"recall_at_{k}": None for k in RETRIEVAL_KS},
            **{f"hit_at_{k}": None for k in RETRIEVAL_KS},
            "mrr": None,
            "first_relevant_rank": None,
        }
    metrics: dict[str, Any] = {"eligible": True}
    for k in RETRIEVAL_KS:
        found = {event_id for result in results[:k] for event_id in _result_evidence_ids(result)}
        hits = found & gold
        metrics[f"recall_at_{k}"] = len(hits) / len(gold)
        metrics[f"hit_at_{k}"] = float(bool(hits))
    first_rank = next(
        (rank for rank, result in enumerate(results, start=1) if set(_result_evidence_ids(result)) & gold),
        None,
    )
    metrics["mrr"] = 1.0 / first_rank if first_rank is not None else 0.0
    metrics["first_relevant_rank"] = first_rank
    return metrics


def retrieval_metrics(
    results: Sequence[Mapping[str, Any]],
    gold_event_ids: Sequence[str],
    *,
    relevance_by_claim_id: Mapping[str, float] | None = None,
    relevance_threshold: float = CLAIM_RELEVANCE_THRESHOLD,
) -> dict[str, Any]:
    """Compute claim-level retrieval metrics plus session-level diagnostics."""
    session = _session_retrieval_metrics(results, gold_event_ids)
    if relevance_by_claim_id is None:
        metrics = dict(session)
    else:
        relevant_ids = {claim_id for claim_id, score in relevance_by_claim_id.items() if score >= relevance_threshold}
        metrics = {"eligible": bool(relevant_ids)}
        ranked_ids = [str(result.get("id")) for result in results]
        for k in RETRIEVAL_KS:
            hits = set(ranked_ids[:k]) & relevant_ids
            metrics[f"recall_at_{k}"] = len(hits) / len(relevant_ids) if relevant_ids else None
            metrics[f"hit_at_{k}"] = float(bool(hits)) if relevant_ids else None
            scores = [relevance_by_claim_id.get(claim_id, 0.0) for claim_id in ranked_ids[:k]]
            metrics[f"max_relevance_at_{k}"] = max(scores, default=0.0)
        first_rank = next(
            (rank for rank, claim_id in enumerate(ranked_ids, start=1) if claim_id in relevant_ids),
            None,
        )
        metrics["mrr"] = 1.0 / first_rank if first_rank is not None else 0.0
        metrics["first_relevant_rank"] = first_rank
        metrics["answer_covered_by_extracted_claims"] = bool(relevant_ids)
        metrics["relevance_threshold"] = relevance_threshold
    metrics["session_eligible"] = session["eligible"]
    for k in RETRIEVAL_KS:
        metrics[f"session_recall_at_{k}"] = session[f"recall_at_{k}"]
        metrics[f"session_hit_at_{k}"] = session[f"hit_at_{k}"]
    metrics["session_mrr"] = session["mrr"]
    metrics["session_first_relevant_rank"] = session["first_relevant_rank"]
    return metrics


def _aggregate_group(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    successful = [result for result in results if not result.get("error")]
    retrieval = [
        result["retrieval"]
        for result in successful
        if isinstance(result.get("retrieval"), Mapping) and result["retrieval"].get("eligible")
    ]
    qa = [
        result["qa"]
        for result in successful
        if isinstance(result.get("qa"), Mapping) and isinstance(result["qa"].get("correct"), bool)
    ]

    def average(field: str, items: Sequence[Mapping[str, Any]]) -> float | None:
        values = [float(item[field]) for item in items if item.get(field) is not None]
        return mean(values) if values else None

    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "failed_cases": len(results) - len(successful),
        "retrieval_eligible_cases": len(retrieval),
        **{f"recall_at_{k}": average(f"recall_at_{k}", retrieval) for k in RETRIEVAL_KS},
        **{f"hit_rate_at_{k}": average(f"hit_at_{k}", retrieval) for k in RETRIEVAL_KS},
        "mrr": average("mrr", retrieval),
        **{f"session_recall_at_{k}": average(f"session_recall_at_{k}", retrieval) for k in RETRIEVAL_KS},
        **{f"session_hit_rate_at_{k}": average(f"session_hit_at_{k}", retrieval) for k in RETRIEVAL_KS},
        "session_mrr": average("session_mrr", retrieval),
        "answer_covered_by_extracted_claims": average("answer_covered_by_extracted_claims", retrieval),
        "qa_evaluated_cases": len(qa),
        "qa_accuracy": mean(float(item["correct"]) for item in qa) if qa else None,
    }


def aggregate_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("question_type") or "uncategorized"), []).append(result)
    return {
        "overall": _aggregate_group(results),
        "by_type": {question_type: _aggregate_group(items) for question_type, items in sorted(grouped.items())},
    }


def _case_fingerprint(case: LongMemEvalCase) -> str:
    payload = {
        "case_id": case.case_id,
        "question_type": case.question_type,
        "question": case.question,
        "answer": case.answer,
        "question_at": case.question_at,
        "gold_event_ids": case.gold_event_ids,
        "gold_session_ids": case.gold_session_ids,
        "sessions": [
            {
                "session_id": session.session_id,
                "event_id": session.event_id,
                "occurred_at": session.occurred_at,
                "messages": session.messages,
            }
            for session in case.sessions
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_case_name(case_id: str) -> str:
    name = _SAFE_NAME_RE.sub("_", case_id).strip("._-")[:72] or "case"
    if name == case_id and case_id not in {".", ".."}:
        return name
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def _case_paths(case_id: str) -> tuple[Path, Path]:
    database = DATABASE_ROOT / f"{_safe_case_name(case_id)}.db"
    return database, database.with_suffix(".manifest.json")


def _compare_case_paths(case_id: str) -> tuple[Path, Path, Path, Path]:
    directory = COMPARE_ROOT / _safe_case_name(case_id)
    return (
        directory / "base.db",
        directory / "base.manifest.json",
        directory / "claims.json",
        directory,
    )


def _backup_claims_file(
    case_id: str,
    claims_path: Path,
    backup_root: Path = LME_12_BACKUP_ROOT,
) -> Path:
    """Copy one claim sidecar without removing any existing cache entries."""
    if not claims_path.is_file():
        raise FileNotFoundError(f"claims sidecar does not exist: {claims_path}")
    destination = backup_root / _safe_case_name(case_id) / "claims.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(claims_path, destination)
    return destination


def _assert_benchmark_path(path: Path) -> None:
    root = DATABASE_ROOT.resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"refusing to modify path outside benchmark directory: {path}")


def _remove_case_artifacts(database_path: Path, manifest_path: Path) -> None:
    for path in (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        manifest_path,
        manifest_path.with_suffix(f"{manifest_path.suffix}.tmp"),
    ):
        _assert_benchmark_path(path)
        path.unlink(missing_ok=True)


def _remove_database_artifacts(database_path: Path) -> None:
    for path in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")):
        _assert_benchmark_path(path)
        path.unlink(missing_ok=True)


def _remove_compare_variants(directory: Path) -> None:
    """Remove disposable embedding variants while preserving base.db and claims.json."""
    for code in EMBEDDING_CONFIGS:
        _remove_database_artifacts(directory / f"{code}.db")


def _decoded_value(value_json: object) -> str:
    text = str(value_json or "")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _claim_values(connection: Any) -> dict[str, str]:
    rows = connection.execute("SELECT id,value_json FROM claims ORDER BY id").fetchall()
    return {str(row["id"]): _decoded_value(row["value_json"]) for row in rows}


def _export_claim_texts(connection: Any, case: LongMemEvalCase) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT id,subject_entity_id,predicate,value_json,index_text,status FROM claims ORDER BY id"
    ).fetchall()
    claims: list[dict[str, Any]] = []
    for row in rows:
        evidence_rows = connection.execute(
            "SELECT evidence_id FROM evidence_links "
            "WHERE derived_type='claim' AND derived_id=? AND evidence_type='event' ORDER BY evidence_id",
            (row["id"],),
        ).fetchall()
        claims.append(
            {
                "claim_id": str(row["id"]),
                "subject": row["subject_entity_id"],
                "predicate": row["predicate"],
                "value": _decoded_value(row["value_json"]),
                "index_text": str(row["index_text"] or _decoded_value(row["value_json"])),
                "status": row["status"],
                "evidence_event_ids": [str(item["evidence_id"]) for item in evidence_rows],
            }
        )
    return {
        "case_id": case.case_id,
        "case_fingerprint": _case_fingerprint(case),
        "claim_count": len(claims),
        "claims": claims,
    }


def _clear_claim_embeddings(connection: Any) -> None:
    connection.execute(
        "UPDATE claims SET embedding_dense=NULL,embedding_sparse=NULL," "embedding_model=NULL,embedding_dim=NULL"
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _reembed_database(
    db_path: Path,
    config: Mapping[str, str | None],
    embedder: Any,
) -> dict[str, Any]:
    """Replace every claim dense vector in a cloned benchmark database."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id,index_text FROM claims ORDER BY id").fetchall()
        texts = [str(row["index_text"] or "") for row in rows]
        vectors = embedder.embed_documents(texts)
        if len(vectors) != len(rows):
            raise ValueError("embedding count does not match claim count")
        for row, vector in zip(rows, vectors, strict=True):
            if len(vector) != int(embedder.dim) * 4:
                raise ValueError(f"unexpected vector dimension for claim {row['id']}")
            connection.execute(
                "UPDATE claims SET embedding_dense=?,embedding_sparse=NULL,"
                "embedding_model=?,embedding_dim=? WHERE id=?",
                (vector, embedder.model, embedder.dim, row["id"]),
            )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    sparse_requested = config.get("output_type") == "sparse"
    return {
        "claims_reembedded": len(rows),
        "embedding_model": embedder.model,
        "embedding_dim": embedder.dim,
        "sparse_requested": sparse_requested,
        "sparse_mode": "dense_only" if sparse_requested else "not_requested",
        "cost": embedder.cost_snapshot() if hasattr(embedder, "cost_snapshot") else None,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _manifest_identity(
    case: LongMemEvalCase,
    settings: Settings,
    *,
    relevance_scorer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the fields that make an ingest/relevance cache reusable."""
    identity: dict[str, Any] = {
        "case_id": case.case_id,
        "case_fingerprint": _case_fingerprint(case),
        "session_count": len(case.sessions),
        "extractor_version": LLM_EXTRACTOR_VERSION,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "embedding_api_mode": settings.embedding_api_mode,
        "embedding_text_type": settings.embedding_text_type,
    }
    if relevance_scorer is not None:
        identity["relevance_scorer"] = dict(relevance_scorer)
    return identity


def _validate_manifest(
    path: Path,
    case: LongMemEvalCase,
    settings: Settings,
    *,
    relevance_scorer: Mapping[str, Any] | None = None,
) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"--skip-ingest requires cache manifest: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = _manifest_identity(case, settings, relevance_scorer=relevance_scorer)
    mismatches = {key: (manifest.get(key), value) for key, value in expected.items() if manifest.get(key) != value}
    if mismatches:
        raise ValueError(f"cached ingest manifest does not match current case/config: {mismatches}")


def _session_content(session: SessionInput) -> dict[str, Any]:
    messages = [dict(message) for message in session.messages]
    text = "\n".join(f"{message['role']}: {message['content']}" for message in messages)
    return {
        "text": text,
        "messages": messages,
        "benchmark_locator": {"session_id": session.session_id},
    }


def _ingest_case(
    connection: Any,
    case: LongMemEvalCase,
    settings: Settings,
    embedder: Any,
    *,
    case_number: int,
    total_hint: str,
) -> dict[str, Any]:
    service = IngestService(connection)
    extractor = make_extractor(settings, require_real=True, connection=connection)
    stats = {
        "sessions": len(case.sessions),
        "extracted_claims": 0,
        "stored_claims": 0,
        "skipped_claims": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    started = time.perf_counter()
    for index, session in enumerate(case.sessions, start=1):
        content = _session_content(session)
        event = {
            "id": session.event_id,
            "idempotency_key": f"longmemeval:{case.case_id}:{session.session_id}",
            "tenant_id": case.namespace,
            "event_type": "message",
            "actor_type": "user",
            "content": content,
            "occurred_at": session.occurred_at,
        }
        service.ingest_event(event)
        claims = extractor.extract(
            content,
            {
                "actor_type": "user",
                "event_type": "message",
                "session_id": session.session_id,
                "occurred_at": session.occurred_at,
            },
        )
        stats["extracted_claims"] += len(claims)
        stats["input_tokens"] += int(getattr(extractor, "last_input_tokens", 0))
        stats["output_tokens"] += int(getattr(extractor, "last_output_tokens", 0))
        stats["total_tokens"] += int(getattr(extractor, "last_usage_tokens", 0))
        now = datetime.now(timezone.utc).isoformat()
        for claim in claims:
            stored = IngestService.store_extracted(
                connection,
                claim,
                event,
                now,
                embedder,
                policy=settings.retention_policy(),
                relation_discovery_mode=settings.relation_discovery_mode,
                index_text_mode=settings.index_text_mode,
            )
            if stored.status == "skipped":
                stats["skipped_claims"] += 1
            else:
                stats["stored_claims"] += 1
        if index == 1 or index % 10 == 0 or index == len(case.sessions):
            print(
                f"[{case_number}/{total_hint}] {case.case_id}: ingest {index}/{len(case.sessions)} "
                f"claims={stats['extracted_claims']}",
                flush=True,
            )
    stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return stats


def _retrieved_payload(results: Sequence[Mapping[str, Any]], case: LongMemEvalCase) -> list[dict[str, Any]]:
    event_to_session = {session.event_id: session.session_id for session in case.sessions}
    payload: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        evidence_ids = _result_evidence_ids(result)
        payload.append(
            {
                "rank": rank,
                "claim_id": result.get("id"),
                "text": result.get("text"),
                "value": result.get("value"),
                "score": result.get("score"),
                "evidence_event_ids": list(evidence_ids),
                "evidence_session_ids": [event_to_session[item] for item in evidence_ids if item in event_to_session],
            }
        )
    return payload


def _recall_case(
    connection: Any,
    case: LongMemEvalCase,
    settings: Settings,
    embedder: Any,
    reranker: Any,
    *,
    relevance_embedder: Any | None = None,
    relevance_by_claim_id: Mapping[str, float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    service = RecallService(
        connection,
        embedder,
        reranker,
        RelationExpansionConfig(
            enabled=settings.relation_expansion_mode == "on",
            max_depth=settings.relation_expansion_max_depth,
        ),
        settings,
        make_query_expander(settings, connection),
    )
    started = time.perf_counter()
    response = service.recall(
        case.question,
        limit=max(RETRIEVAL_KS),
        as_of=case.question_at,
        namespace=case.namespace,
        debug=True,
    )
    raw_results = response.get("results") or []
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)]
    claim_values = _claim_values(connection)
    for result in results:
        claim_id = str(result.get("id"))
        result["value"] = claim_values.get(claim_id)
    if relevance_by_claim_id is None and case.answer.strip():
        scorer = relevance_embedder or embedder
        relevance_by_claim_id = _claim_relevance_scores(claim_values, case.answer, scorer)
    metrics = retrieval_metrics(
        results,
        case.gold_event_ids,
        relevance_by_claim_id=relevance_by_claim_id if case.answer.strip() else None,
    )
    metrics.update(
        retrieved_claims=len(results),
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )
    return metrics, _retrieved_payload(results, case)


def _structured_mode(settings: Settings) -> StructuredOutputMode:
    return (
        StructuredOutputMode.JSON_OBJECT
        if settings.llm_structured_mode == "json_object"
        else StructuredOutputMode.JSON_SCHEMA
    )


def _response_object(content: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) >= 3 else text
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("LLM structured response must be a JSON object")
    return payload


def _run_qa(
    connection: Any,
    case: LongMemEvalCase,
    retrieved: Sequence[Mapping[str, Any]],
    settings: Settings,
) -> dict[str, Any]:
    mode = _structured_mode(settings)
    reader = make_llm_client(settings, connection, operation="benchmark_reader", model=QA_MODEL)
    judge = make_llm_client(settings, connection, operation="benchmark_judge", model=QA_MODEL)
    context = "\n".join(f"[{item['rank']}] {item.get('text') or ''}" for item in retrieved)
    reader_spec = StructuredOutputSpec(
        name="longmemeval_reader_answer",
        preferred_mode=mode,
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    reader_response = reader.complete(
        LLMRequest(
            messages=[
                LLMMessage(
                    "system",
                    "Answer the question using only the supplied memory claims. "
                    "If the claims do not contain the answer, say that the information is unavailable.",
                ),
                LLMMessage("user", f"Memory claims:\n{context or '(none)'}\n\nQuestion: {case.question}"),
            ],
            structured_output=reader_spec,
        )
    )
    predicted = str(_response_object(reader_response.content).get("answer") or "").strip()

    judge_spec = StructuredOutputSpec(
        name="longmemeval_answer_judgment",
        preferred_mode=mode,
        schema={
            "type": "object",
            "properties": {
                "correct": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["correct", "reason"],
            "additionalProperties": False,
        },
    )
    judge_response = judge.complete(
        LLMRequest(
            messages=[
                LLMMessage(
                    "system",
                    "Judge whether the candidate answer is semantically correct relative to the reference answer. "
                    "Allow paraphrases, but reject contradictions or missing required facts.",
                ),
                LLMMessage(
                    "user",
                    f"Question: {case.question}\nReference answer: {case.answer}\nCandidate answer: {predicted}",
                ),
            ],
            structured_output=judge_spec,
        )
    )
    judgment = _response_object(judge_response.content)
    if not isinstance(judgment.get("correct"), bool):
        raise ValueError("judge response is missing boolean 'correct'")
    return {
        "model": QA_MODEL,
        "predicted_answer": predicted,
        "correct": judgment["correct"],
        "reason": str(judgment.get("reason") or ""),
        "usage": {
            "reader_tokens": reader_response.usage_total_tokens,
            "judge_tokens": judge_response.usage_total_tokens,
            "total_tokens": reader_response.usage_total_tokens + judge_response.usage_total_tokens,
        },
    }


def _run_case(
    case: LongMemEvalCase,
    settings: Settings,
    embedder: Any,
    reranker: Any,
    *,
    skip_ingest: bool,
    run_qa: bool,
    clean: bool,
    case_number: int,
    total_hint: str,
) -> dict[str, Any]:
    database_path, manifest_path = _case_paths(case.case_id)
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "question_type": case.question_type,
        "question": case.question,
        "answer": case.answer,
        "session_count": len(case.sessions),
        "gold_session_ids": list(case.gold_session_ids),
        "database": str(database_path.relative_to(ROOT)),
        "ingest": None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "error": None,
    }
    database: Database | None = None
    started = time.perf_counter()
    try:
        DATABASE_ROOT.mkdir(parents=True, exist_ok=True)
        if skip_ingest:
            if not database_path.is_file():
                raise FileNotFoundError(f"--skip-ingest requires cached database: {database_path}")
            _validate_manifest(manifest_path, case, settings)
            result["ingest"] = {"skipped": True, "cache_manifest": str(manifest_path.relative_to(ROOT))}
        else:
            _remove_case_artifacts(database_path, manifest_path)

        database = Database(database_path, settings=settings)
        connection = database.open()
        if not skip_ingest:
            result["ingest"] = _ingest_case(
                connection,
                case,
                settings,
                embedder,
                case_number=case_number,
                total_hint=total_hint,
            )
            _write_json_atomic(
                manifest_path,
                {
                    **_manifest_identity(case, settings),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        result["retrieval"], result["retrieved"] = _recall_case(
            connection,
            case,
            settings,
            embedder,
            reranker,
        )
        if run_qa:
            result["qa"] = _run_qa(connection, case, result["retrieved"], settings)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if database is not None:
            database.close()
        if clean:
            _remove_case_artifacts(database_path, manifest_path)
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def _remove_compare_case_artifacts(case: LongMemEvalCase) -> None:
    base_path, manifest_path, claims_path, directory = _compare_case_paths(case.case_id)
    _remove_database_artifacts(base_path)
    _remove_compare_variants(directory)
    for path in (
        manifest_path,
        claims_path,
        manifest_path.with_suffix(".json.tmp"),
        claims_path.with_suffix(".json.tmp"),
    ):
        _assert_benchmark_path(path)
        path.unlink(missing_ok=True)
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


def _prepare_compare_base(
    case: LongMemEvalCase,
    settings: Settings,
    production_embedder: Any,
    relevance_embedder: Any | None,
    *,
    skip_ingest: bool,
    case_number: int,
    total_hint: str,
) -> dict[str, Any]:
    base_path, manifest_path, claims_path, _ = _compare_case_paths(case.case_id)
    relevance_scorer = _relevance_scorer_metadata()
    if skip_ingest:
        if not base_path.is_file() or not claims_path.is_file():
            raise FileNotFoundError(f"--skip-ingest requires base database and claims sidecar for {case.case_id}")
        _validate_manifest(manifest_path, case, settings, relevance_scorer=relevance_scorer)
        payload = json.loads(claims_path.read_text(encoding="utf-8"))
        if payload.get("relevance", {}).get("scorer") != relevance_scorer:
            raise ValueError(f"cached relevance scorer does not match current config for {case.case_id}")
        backup_path = _backup_claims_file(case.case_id, claims_path)
        return {
            "database": str(base_path.relative_to(ROOT)),
            "claims_file": str(claims_path.relative_to(ROOT)),
            "ingest": {"skipped": True},
            "claim_count": int(payload.get("claim_count", 0)),
            "claims_backup": str(backup_path.relative_to(ROOT)),
            "relevance_by_claim_id": {
                str(item["claim_id"]): float(item.get("relevance_score", 0.0)) for item in payload.get("claims", [])
            },
        }

    _remove_compare_case_artifacts(case)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(base_path, settings=settings)
    try:
        connection = database.open()
        ingest = _ingest_case(
            connection,
            case,
            settings,
            production_embedder,
            case_number=case_number,
            total_hint=total_hint,
        )
        payload = _export_claim_texts(connection, case)
        if relevance_embedder is None:
            raise ValueError("fixed relevance embedder is required when preparing a fresh compare base")
        payload["claims"] = _claim_similarity_records(payload["claims"], case, relevance_embedder)
        relevance = {str(item["claim_id"]): float(item.get("answer_similarity") or 0.0) for item in payload["claims"]}
        for item in payload["claims"]:
            item["relevance_score"] = relevance[str(item["claim_id"])]
        payload["relevance"] = {
            "method": ["cosine(claim_value, answer)", "cosine(claim_value, question)"],
            "scorer": relevance_scorer,
        }
        _write_json_atomic(claims_path, payload)
        backup_path = _backup_claims_file(case.case_id, claims_path)
        _clear_claim_embeddings(connection)
        _write_json_atomic(
            manifest_path,
            {
                **_manifest_identity(case, settings, relevance_scorer=relevance_scorer),
                "vectors_cleared": True,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    finally:
        database.close()
    return {
        "database": str(base_path.relative_to(ROOT)),
        "claims_file": str(claims_path.relative_to(ROOT)),
        "ingest": ingest,
        "claim_count": int(payload["claim_count"]),
        "claims_backup": str(backup_path.relative_to(ROOT)),
        "relevance_by_claim_id": relevance,
    }


def _clone_base_database(base_path: Path, variant_path: Path) -> None:
    if not base_path.is_file():
        raise FileNotFoundError(f"base benchmark database is missing: {base_path}")
    variant_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_database_artifacts(variant_path)
    shutil.copy2(base_path, variant_path)


def _config_case_result(
    case: LongMemEvalCase,
    settings: Settings,
    embedder: ConfigCompareEmbedder,
    reranker: Any,
    definition: Mapping[str, str | None],
    base: Mapping[str, Any],
    *,
    clean: bool,
) -> dict[str, Any]:
    base_path, _, _, directory = _compare_case_paths(case.case_id)
    variant_path = directory / f"{embedder.config.code}.db"
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "question_type": case.question_type,
        "question": case.question,
        "answer": case.answer,
        "session_count": len(case.sessions),
        "gold_session_ids": list(case.gold_session_ids),
        "database": str(variant_path.relative_to(ROOT)),
        "reembedding": None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "error": None,
    }
    database: Database | None = None
    started = time.perf_counter()
    try:
        _clone_base_database(base_path, variant_path)
        result["reembedding"] = _reembed_database(variant_path, definition, embedder)
        database = Database(variant_path, settings=settings)
        connection = database.open()
        result["retrieval"], result["retrieved"] = _recall_case(
            connection,
            case,
            settings,
            embedder,
            reranker,
            relevance_by_claim_id=base["relevance_by_claim_id"],
        )
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if database is not None:
            database.close()
        if clean:
            _remove_database_artifacts(variant_path)
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def _config_comparison_rows(configs: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "recall_at_1",
        "recall_at_5",
        "recall_at_10",
        "mrr",
        "session_recall_at_1",
        "session_recall_at_5",
        "session_recall_at_10",
        "session_mrr",
    )
    return [
        {
            "metric": field,
            **{code: payload["metrics"]["overall"].get(field) for code, payload in configs.items()},
        }
        for field in fields
    ]


def _config_compare_report(
    args: argparse.Namespace,
    settings: Settings,
    cases: Sequence[LongMemEvalCase],
    extraction: Sequence[Mapping[str, Any]],
    configs: Mapping[str, Mapping[str, Any]],
    started_at: str,
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "benchmark": "LongMemEval-S",
        "mode": "extract_once_embedding_config_compare",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(args.dataset.resolve()),
            "bytes": args.dataset.stat().st_size,
            "complete_json_array": _dataset_complete(args.dataset),
            "sampling": "first N per question type",
            "question_types": list(QUESTION_TYPES),
            "case_ids": [case.case_id for case in cases],
        },
        "run": {
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "limit": len(cases),
            "skip_ingest": args.skip_ingest,
            "qa_enabled": False,
            "clean": args.clean,
            "extractor": settings.llm_model,
            "extractor_version": LLM_EXTRACTOR_VERSION,
            "relevance_scorer": _relevance_scorer_metadata(),
            "relevance_threshold": CLAIM_RELEVANCE_THRESHOLD,
            "primary_metric_relevance": (
                "claim Recall@K = relevant claims retrieved / all relevant extracted claims; " "Hit@K remains binary"
            ),
            "auxiliary_metric_relevance": "claim evidence links to answer session",
            "q4_sparse": "requested from API but not indexed; dense component only",
        },
        "extraction": list(extraction),
        "configs": dict(configs),
        "comparison": _config_comparison_rows(configs) if configs else [],
    }


def _run_config_compare(
    args: argparse.Namespace,
    settings: Settings,
    production_embedder: Any,
    reranker: Any,
    started_at: str,
) -> int:
    if not args.no_qa:
        raise ValueError("--config-compare is retrieval-only; pass --no-qa")
    selected_limit = args.limit or 12
    if selected_limit % len(QUESTION_TYPES):
        raise ValueError(f"--config-compare limit must be divisible by {len(QUESTION_TYPES)} for stratified sampling")
    n_per_type = selected_limit // len(QUESTION_TYPES)
    records = list(iter_case_records(args.dataset))
    cases = [normalize_case(record) for record in _sample_stratified(records, n_per_type=n_per_type)]
    total_hint = str(len(cases))
    bases: dict[str, dict[str, Any]] = {}
    extraction: list[dict[str, Any]] = []
    configs: dict[str, dict[str, Any]] = {}
    relevance_client: DashScopeEmbeddingClient | None = None
    relevance_embedder: ConfigCompareEmbedder | None = None
    if not args.skip_ingest:
        relevance_client = DashScopeEmbeddingClient(
            str(settings.embedding_api_key),
            base_url=settings.embedding_base_url,
            timeout_seconds=max(90.0, settings.embedding_read_timeout),
            max_attempts=settings.embedding_max_attempts,
            trust_env=False,
        )
        relevance_definition = EMBEDDING_CONFIGS[RELEVANCE_SCORER_CODE]
        relevance_embedder = ConfigCompareEmbedder(
            relevance_client,
            _embedding_config(RELEVANCE_SCORER_CODE, relevance_definition),
            COMPARE_CACHE / "relevance",
        )

    print(
        f"config-compare extract-once cases={len(cases)} types={len(QUESTION_TYPES)} "
        f"extractor={settings.llm_model} prompt={LLM_EXTRACTOR_VERSION}",
        flush=True,
    )
    try:
        for case_number, case in enumerate(cases, start=1):
            base = _prepare_compare_base(
                case,
                settings,
                production_embedder,
                relevance_embedder,
                skip_ingest=args.skip_ingest,
                case_number=case_number,
                total_hint=total_hint,
            )
            bases[case.case_id] = base
            extraction.append({key: value for key, value in base.items() if key != "relevance_by_claim_id"})
            _write_json_atomic(
                args.output,
                _config_compare_report(args, settings, cases, extraction, configs, started_at, "extracting"),
            )
    finally:
        if relevance_client is not None:
            relevance_client.close()

    client = DashScopeEmbeddingClient(
        str(settings.embedding_api_key),
        base_url=settings.embedding_base_url,
        timeout_seconds=max(90.0, settings.embedding_read_timeout),
        max_attempts=settings.embedding_max_attempts,
        trust_env=False,
    )
    try:
        for code, definition in EMBEDDING_CONFIGS.items():
            variant = _embedding_config(code, definition)
            embedder = ConfigCompareEmbedder(client, variant, COMPARE_CACHE)
            case_results: list[dict[str, Any]] = []
            print(
                f"[{code}] model={variant.model} api={variant.api_kind} "
                f"text_type={variant.use_text_type} instruct={variant.use_instruct} "
                f"sparse={variant.use_sparse}",
                flush=True,
            )
            for case_number, case in enumerate(cases, start=1):
                case_result = _config_case_result(
                    case,
                    settings,
                    embedder,
                    reranker,
                    definition,
                    bases[case.case_id],
                    clean=args.clean,
                )
                case_results.append(case_result)
                retrieval = case_result.get("retrieval") or {}
                print(
                    f"[{code} {case_number}/{len(cases)}] {case.case_id}: "
                    f"R@10={retrieval.get('recall_at_10')} MRR={retrieval.get('mrr')} "
                    f"session_R@10={retrieval.get('session_recall_at_10')} "
                    f"error={case_result.get('error')}",
                    flush=True,
                )
            configs[code] = {
                "definition": dict(definition),
                "effective": {
                    "dimension": variant.dim,
                    "text_type_enabled": variant.use_text_type,
                    "query_instruct_enabled": variant.use_instruct,
                    "sparse_requested": variant.use_sparse,
                    "retrieval_vector_mode": "dense_only",
                    "sparse_rows_received": embedder.sparse_rows_received,
                },
                "embedding_cost": embedder.cost_snapshot(),
                "metrics": aggregate_results(case_results),
                "cases": case_results,
            }
            _write_json_atomic(
                args.output,
                _config_compare_report(args, settings, cases, extraction, configs, started_at, "running"),
            )
    finally:
        client.close()
        if args.clean:
            for case in cases:
                _, _, _, directory = _compare_case_paths(case.case_id)
                _remove_compare_variants(directory)

    report = _config_compare_report(
        args,
        settings,
        cases,
        extraction,
        configs,
        started_at,
        "completed",
    )
    _write_json_atomic(args.output, report)
    threshold_payloads = {
        case.case_id: json.loads(_compare_case_paths(case.case_id)[2].read_text(encoding="utf-8")) for case in cases
    }
    _write_json_atomic(THRESHOLD_ANALYSIS_OUTPUT, _threshold_analysis(cases, threshold_payloads))
    headers = "metric " + " ".join(f"{code:>8}" for code in EMBEDDING_CONFIGS)
    print(headers, flush=True)
    for row in report["comparison"]:
        values = " ".join(
            f"{float(row[code]):8.4f}" if row.get(code) is not None else f"{'n/a':>8}" for code in EMBEDDING_CONFIGS
        )
        print(f"{row['metric']:<24} {values}", flush=True)
    failures = sum(payload["metrics"]["overall"]["failed_cases"] for payload in configs.values())
    print(f"completed config-compare failures={failures} output={args.output}", flush=True)
    return 1 if failures else 0


def _dataset_complete(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 4096))
        return stream.read().rstrip().endswith(b"]")


def _report(
    args: argparse.Namespace,
    settings: Settings,
    results: Sequence[Mapping[str, Any]],
    started_at: str,
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(args.dataset.resolve()),
            "bytes": args.dataset.stat().st_size if args.dataset.is_file() else None,
            "complete_json_array": _dataset_complete(args.dataset),
        },
        "run": {
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "limit": args.limit,
            "skip_ingest": args.skip_ingest,
            "qa_enabled": not args.no_qa,
            "clean": args.clean,
            "config_compare": args.config_compare,
            "models": {
                "extractor": settings.llm_model,
                "extractor_version": LLM_EXTRACTOR_VERSION,
                "embedder": settings.embedding_model,
                "embedding_api_mode": settings.embedding_api_mode,
                "embedding_text_type": settings.embedding_text_type,
                "reranker": settings.reranker_model if settings.reranker_mode != "off" else "off",
                "reader": QA_MODEL if not args.no_qa else "not_run",
                "judge": QA_MODEL if not args.no_qa else "not_run",
            },
            "retrieval_k": list(RETRIEVAL_KS),
            "metric_relevance": {
                "label": (
                    "claim is relevant when cosine(claim value, reference answer) " f">= {CLAIM_RELEVANCE_THRESHOLD:g}"
                ),
                "recall_at_k": "relevant claims retrieved in top-k / all relevant extracted claims",
                "hit_at_k": "binary: at least one relevant claim appears in top-k",
                "auxiliary": "claim evidence links to answer_session_ids",
            },
        },
        "metrics": aggregate_results(results),
        "cases": list(results),
    }


def _validate_production_settings(settings: Settings) -> None:
    if settings.llm_model != QA_MODEL:
        raise ValueError(f"llm.model must be {QA_MODEL}, found {settings.llm_model}")
    if settings.embedder_mode != "real":
        raise ValueError("embedding.mode must be real")
    if settings.embedding_model != "qwen3.7-text-embedding":
        raise ValueError("embedding.model must be qwen3.7-text-embedding, " f"found {settings.embedding_model}")
    if settings.embedding_api_mode != "native":
        raise ValueError(f"embedding.api_mode must be native, found {settings.embedding_api_mode}")
    if not settings.embedding_api_key:
        raise ValueError("EMBEDDING_API_KEY is required")
    if not settings.llm_api_key:
        raise ValueError("LLM_API_KEY is required")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be a positive integer")
    if args.limit is None and not _dataset_complete(args.dataset):
        raise ValueError(
            "LongMemEval dataset is missing or incomplete; wait for the top-level JSON array to finish downloading"
        )
    settings = load_settings(args.config, args.env_file)
    settings = dataclasses.replace(settings, vector_backend="sqlite_scan")
    _validate_production_settings(settings)
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    started_at = datetime.now(timezone.utc).isoformat()
    if args.config_compare:
        return _run_config_compare(args, settings, embedder, reranker, started_at)
    results: list[dict[str, Any]] = []
    total_hint = str(args.limit) if args.limit is not None else "all"

    print(
        f"LongMemEval-S model={settings.llm_model} embedder={settings.embedding_model} "
        f"prompt={LLM_EXTRACTOR_VERSION} limit={total_hint} qa={not args.no_qa}",
        flush=True,
    )
    try:
        for case_number, record in enumerate(iter_case_records(args.dataset, args.limit), start=1):
            case = normalize_case(record)
            case_result = _run_case(
                case,
                settings,
                embedder,
                reranker,
                skip_ingest=args.skip_ingest,
                run_qa=not args.no_qa,
                clean=args.clean,
                case_number=case_number,
                total_hint=total_hint,
            )
            results.append(case_result)
            _write_json_atomic(args.output, _report(args, settings, results, started_at, "running"))
            retrieval = case_result.get("retrieval") or {}
            print(
                f"[{case_number}/{total_hint}] {case.case_id}: "
                f"R@10={retrieval.get('recall_at_10')} MRR={retrieval.get('mrr')} "
                f"error={case_result.get('error')}",
                flush=True,
            )
    except Exception:
        if results:
            _write_json_atomic(args.output, _report(args, settings, results, started_at, "aborted"))
        raise

    if not results:
        raise ValueError("LongMemEval dataset contains no selected cases")
    report = _report(args, settings, results, started_at, "completed")
    _write_json_atomic(args.output, report)
    overall = report["metrics"]["overall"]
    print(
        f"completed cases={overall['cases']} failures={overall['failed_cases']} "
        f"R@1={overall['recall_at_1']} R@5={overall['recall_at_5']} "
        f"R@10={overall['recall_at_10']} MRR={overall['mrr']} "
        f"QA={overall['qa_accuracy']} output={args.output}",
        flush=True,
    )
    return 1 if overall["failed_cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
