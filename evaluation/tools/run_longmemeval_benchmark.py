#!/usr/bin/env python
"""Run LongMemEval-S against hl_mem's production extraction and recall stack."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean, median
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# These imports intentionally follow the ROOT bootstrap so the runner remains
# directly executable from outside the repository root.
# isort: off
from evaluation.tools.longmemeval.judge import (  # noqa: E402, F401
    LONGMEMEVAL_JUDGE_PROMPT_VERSION,
    judge_longmemeval_answer as _judge_longmemeval_answer_impl,
    longmemeval_judge_prompts as _longmemeval_judge_prompts,
)
from evaluation.tools.longmemeval.extraction_fragments import fragment_turn_content  # noqa: E402, F401
from evaluation.tools.longmemeval.full_context import (  # noqa: E402, F401
    render_full_context_user_prompt,
)
from evaluation.tools.longmemeval.native_rag import (  # noqa: E402, F401
    render_native_rag_user_prompt,
    render_raw_session_documents,
    select_raw_sessions,
)
from evaluation.tools.longmemeval.qa_client import (  # noqa: E402, F401
    QAUsage,
    qa_call_with_retry,
    qa_dashscope_chat as _qa_dashscope_chat,
    qa_dashscope_chat_detailed as _qa_dashscope_chat_detailed,
    qa_model,
    response_object as _response_object,
)
from evaluation.tools.longmemeval.reader_context import (  # noqa: E402, F401
    DEFAULT_READER_CONTEXT_MODE,
    QA_ADJACENT_TURN_TOKEN_LIMIT,
    QA_CLAIM_FIELD_TOKEN_LIMIT,
    QA_CLAIMS_TOKEN_BUDGET,
    QA_CONTEXT_TOKEN_BUDGET,
    QA_EVIDENCE_EVENT_TOKEN_LIMIT,
    QA_EVIDENCE_MAX_WINDOWS,
    QA_EVIDENCE_TURN_RADIUS,
    QA_MATCHED_TURN_TOKEN_LIMIT,
    READER_CONTEXT_MODES,
    build_reader_user_prompt as _build_reader_user_prompt,
    event_content_text as _event_content_text,
    fit_reader_claim as _fit_reader_claim,
    fit_reader_claims as _fit_reader_claims,
    fit_reader_event as _fit_reader_event,
    load_reader_events as _load_reader_events,
    normalize_content as _normalize_content,
    normalize_role as _normalize_role,
    ordered_evidence_ids as _ordered_evidence_ids,
    reader_claim_records as _reader_claim_records,
    reader_event_needles as _reader_event_needles,
    reader_focus_index as _reader_focus_index,
    reader_match_score as _reader_match_score,
    reader_match_text as _reader_match_text,
    reader_match_units as _reader_match_units,
    reader_messages as _reader_messages,
    reader_turn_excerpt as _reader_turn_excerpt,
    reader_turn_score as _reader_turn_score,
    reader_turn_window as _reader_turn_window,
    render_reader_user_prompt as _render_reader_user_prompt,
    truncate_reader_text as _truncate_reader_text,
)
from evaluation.tools.run_embedding_ablation import (  # noqa: E402, F401
    Cost,
    DashScopeEmbeddingClient,
    EmbeddingConfig,
    embed_remote,
)
from evaluation.tools.http_diagnostics import (  # noqa: E402, F401
    evaluation_http_error_diagnostics as _evaluation_http_error_diagnostics,
    sanitize_diagnostic_text,
)
from hl_mem import __version__  # noqa: E402, F401
from hl_mem.application.context_packet import estimate_tokens  # noqa: E402, F401
from hl_mem.application.ingest import IngestService  # noqa: E402, F401
from hl_mem.application.recall import RecallService  # noqa: E402, F401
from hl_mem.components import (  # noqa: E402, F401
    initialize_process,
    make_embedder,
    make_extractor,
    make_query_expander,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402, F401
from hl_mem.core.vector import cosine_similarity, pack_vector  # noqa: E402, F401
from hl_mem.domain.recall import RecallIntent  # noqa: E402, F401
from hl_mem.http_utils import (  # noqa: E402, F401
    exception_chain as _exception_chain,
    find_http_exception,
    find_http_status_error as _find_http_status_error,
    retry_after_seconds as _retry_after_seconds,
)
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION  # noqa: E402, F401
from hl_mem.observability.audit import NullAuditLogger  # noqa: E402
from hl_mem.recall.relation_expansion import RelationExpansionConfig  # noqa: E402, F401
from hl_mem.settings import Settings  # noqa: E402, F401
from hl_mem.storage.database import Database  # noqa: E402, F401
from hl_mem.workers.consolidate import auto_resolve_conflicts  # noqa: E402
from hl_mem.workers.deduplicate import review_pending_near_duplicates  # noqa: E402
from hl_mem.workers.worker import Worker  # noqa: E402

# isort: on

DEFAULT_DATASET = ROOT / "evaluation" / "longmemeval" / "longmemeval_s_cleaned.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "longmemeval_s_benchmark.json"
DEFAULT_FULL_CONTEXT_OUTPUT = ROOT / "evaluation" / "results" / "longmemeval_fullcontext_control.json"
DEFAULT_NATIVE_RAG_OUTPUT = ROOT / "evaluation" / "results" / "longmemeval_nativerag_control.json"
DATABASE_ROOT = ROOT / "var" / "benchmark_lme"
NATIVE_RAG_CACHE_ROOT = ROOT / "evaluation" / "cache" / "longmemeval_native_rag"
COMPARE_ROOT = DATABASE_ROOT / "config_compare"
COMPARE_CACHE = ROOT / "evaluation" / "cache" / "longmemeval_config_compare"
LME_12_BACKUP_ROOT = ROOT / "evaluation" / "cache" / "lme_12_backup"
THRESHOLD_ANALYSIS_OUTPUT = ROOT / "evaluation" / "results" / "lme_12_threshold_analysis.json"
DEFAULT_CONFIG = ROOT / "hl_mem.toml"
DEFAULT_ENV_FILE = ROOT / ".env"
QA_MAX_ATTEMPTS = 3
READER_ANSWER_TOKEN_BUDGET = 512
READER_THINKING_TOKEN_BUDGET = 2048
FULL_CONTEXT_READER_TIMEOUT_SECONDS = 300.0
FULL_CONTEXT_PROTOCOL_VERSION = "full-context-raw-sessions-v1"
NATIVE_RAG_PROTOCOL_VERSION = "raw-session-dense-rag-v1"
NATIVE_RAG_TOP_K = 10
NATIVE_RAG_EMBEDDING_TIMEOUT_SECONDS = 90.0
DEFAULT_FAIL_STOP_COUNT = 5
BENCHMARK_EVENT_MODEL_VERSION = "turn-events-v1"
EXTRACTION_FRAGMENT_PROTOCOL_VERSION = "production-microbatch-v1"
READER_CONTEXT_PROTOCOL_VERSION = "session-turn-window-v2"
BENCHMARK_MAINTENANCE_PROTOCOL_VERSION = "deterministic-dedup-conflicts-v1"
CLAIM_RESTATEMENT_LEXICAL_THRESHOLD = 0.82
RETRIEVAL_KS = (1, 5, 10)
READER_EVIDENCE_LIMIT = 10
_DEEPSEEK_V4_FLASH_INPUT_CNY_PER_MILLION = 1.0
_DEEPSEEK_V4_FLASH_OUTPUT_CNY_PER_MILLION = 2.0
_QWEN37_EMBEDDING_INPUT_CNY_PER_MILLION = 0.5
JSON_READ_CHARS = 1024 * 1024
FALLBACK_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_RECOMMENDATION_QUESTION_RE = re.compile(
    r"(?ix)(?:\b(?:recommend|suggest|advice|ideas?|resources?|options?)\b|"
    r"\b(?:help\s+(?:me|us)\s+)?(?:choose|pick)\b|\bshould\s+(?:i|we)\b|"
    r"推荐|建议|主意|资源|帮(?:我|我们)?(?:选择|挑选))"
)
_COUNT_OR_SUM_QUESTION_RE = re.compile(
    r"(?ix)(?:\b(?:how\s+many|total|sum|altogether|combined)\b|多少|几个|总共|合计|求和)"
)
CLAIM_RELEVANCE_THRESHOLD = 0.5
SIMILARITY_THRESHOLDS = (0.2, 0.3, 0.4, 0.5, 0.65)
RELEVANCE_SCORER_CODE = "V0"
RELEVANCE_LABEL_VERSION = "claim-answer-cosine-v2"
_T = TypeVar("_T")
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
    """One LongMemEval session with a stable anchor for per-turn events."""

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
        return cast(dict[str, int | float], self.cost.as_dict())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("hl-mem", "full-context", "native-rag"),
        default="hl-mem",
        help="run hl_mem, retrieval-free full history, or raw-session dense RAG",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0, help="skip the first N dataset cases before applying --limit")
    parser.add_argument("--resume", action="store_true", help="preserve and skip case_ids already present in --output")
    parser.add_argument(
        "--max-runtime-hours",
        type=float,
        help="stop gracefully at the next case boundary after this many hours",
    )
    parser.add_argument(
        "--fail-stop-count",
        type=int,
        default=DEFAULT_FAIL_STOP_COUNT,
        help="abort after this many consecutive cases fail with the same error type",
    )
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--no-qa", action="store_true")
    parser.add_argument(
        "--reader-context-mode",
        choices=READER_CONTEXT_MODES,
        default=DEFAULT_READER_CONTEXT_MODE,
        help="reader evidence packing: query-aware turn windows (default) or legacy event head truncation",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--config-compare",
        action="store_true",
        help="extract once, then compare V0/Q0/Q1/Q2/Q3/Q4 on a stratified sample",
    )
    args = parser.parse_args(argv)
    if args.output is None:
        if args.mode == "full-context":
            args.output = DEFAULT_FULL_CONTEXT_OUTPUT
        elif args.mode == "native-rag":
            args.output = DEFAULT_NATIVE_RAG_OUTPUT
        else:
            args.output = DEFAULT_OUTPUT
    return args


def iter_case_records(
    path: Path,
    limit: int | None = None,
    offset: int = 0,
) -> Iterator[dict[str, Any]]:
    """Stream a top-level JSON array and stop without reading its unused tail."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer")
    if offset < 0:
        raise ValueError("--offset must be a non-negative integer")
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

        skipped = 0
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
            position = end
            if skipped < offset:
                skipped += 1
                continue
            yield record
            yielded += 1
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


def _turn_event_id(session_event_id: str, turn_index: int) -> str:
    """Derive one stable event ID for a source turn within a session."""
    return f"{session_event_id}:turn:{turn_index:03d}"


def _event_to_session(case: LongMemEvalCase) -> dict[str, str]:
    return {
        _turn_event_id(session.event_id, turn_index): session.session_id
        for session in case.sessions
        for turn_index in range(len(session.messages))
    }


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
    gold_session_events = tuple(dict.fromkeys(event_id for token in evidence_tokens for event_id in aliases[token]))
    session_by_event = {session.event_id: session for session in sessions}
    gold_session_ids = tuple(dict.fromkeys(session_by_event[event_id].session_id for event_id in gold_session_events))
    gold_event_ids = tuple(
        _turn_event_id(event_id, turn_index)
        for event_id in gold_session_events
        for turn_index in range(len(session_by_event[event_id].messages))
    )
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


def _evaluation_eligibility(case: LongMemEvalCase) -> dict[str, Any]:
    """Separate precise-timestamp contradictions from temporal retrieval quality."""
    eligible: dict[str, Any] = {
        "status": "eligible",
        "temporal_gate_eligible": True,
        "reason_code": None,
    }
    if "temporal" not in case.question_type.casefold() or not case.question_at:
        return eligible
    try:
        question_at = datetime.fromisoformat(case.question_at.replace("Z", "+00:00"))
        gold_sessions = [session for session in case.sessions if session.session_id in case.gold_session_ids]
        gold_times = [datetime.fromisoformat(session.occurred_at.replace("Z", "+00:00")) for session in gold_sessions]
    except (TypeError, ValueError):
        return eligible
    if question_at.tzinfo is None:
        question_at = question_at.replace(tzinfo=timezone.utc)
    gold_times = [item if item.tzinfo is not None else item.replace(tzinfo=timezone.utc) for item in gold_times]
    if gold_times and all(
        item > question_at and item.astimezone(question_at.tzinfo).date() == question_at.date() for item in gold_times
    ):
        return {
            "status": "invalid_ambiguous",
            "temporal_gate_eligible": False,
            "reason_code": "gold_sessions_after_question_same_day",
            "question_at": case.question_at,
            "earliest_gold_session_at": min(gold_times).isoformat(),
        }
    return eligible


def _longmemeval_recall_intent(case: LongMemEvalCase) -> RecallIntent:
    """Map benchmark semantics to an explicit recall intent."""
    question_type = case.question_type.casefold()
    if "preference" in question_type:
        return RecallIntent.PREFERENCE
    if "temporal" in question_type:
        return RecallIntent.HISTORICAL
    return RecallIntent.CURRENT_STATE


def _reader_recall_limit(_case: LongMemEvalCase) -> int:
    return READER_EVIDENCE_LIMIT


def _production_order_top_k(
    results: Sequence[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Keep the production RecallService order when building reader evidence."""
    return list(results[:limit])


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
    event_to_session = _event_to_session(case)
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
    *,
    gold_session_ids: Sequence[str] | None = None,
    event_to_session: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    gold = set(gold_session_ids if gold_session_ids is not None else gold_event_ids)
    if not gold:
        return {
            "eligible": False,
            **{f"recall_at_{k}": None for k in RETRIEVAL_KS},
            **{f"hit_at_{k}": None for k in RETRIEVAL_KS},
            "mrr": None,
            "first_relevant_rank": None,
        }
    metrics: dict[str, Any] = {"eligible": True}

    def result_sessions(result: Mapping[str, Any]) -> set[str]:
        evidence_ids = _result_evidence_ids(result)
        if gold_session_ids is None:
            return set(evidence_ids)
        mapping = event_to_session or {}
        return {mapping[event_id] for event_id in evidence_ids if event_id in mapping}

    for k in RETRIEVAL_KS:
        found = {session_id for result in results[:k] for session_id in result_sessions(result)}
        hits = found & gold
        metrics[f"recall_at_{k}"] = len(hits) / len(gold)
        metrics[f"hit_at_{k}"] = float(bool(hits))
    first_rank = next(
        (rank for rank, result in enumerate(results, start=1) if result_sessions(result) & gold),
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
    gold_session_ids: Sequence[str] | None = None,
    event_to_session: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compute claim-level retrieval metrics plus session-level diagnostics."""
    session = _session_retrieval_metrics(
        results,
        gold_event_ids,
        gold_session_ids=gold_session_ids,
        event_to_session=event_to_session,
    )
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
    gate_eligible = [
        result
        for result in results
        if not isinstance(result.get("evaluation_eligibility"), Mapping)
        or result["evaluation_eligibility"].get("temporal_gate_eligible") is not False
    ]
    gate_successful = [result for result in gate_eligible if not result.get("error")]
    gate_excluded = [
        result
        for result in results
        if isinstance(result.get("evaluation_eligibility"), Mapping)
        and result["evaluation_eligibility"].get("temporal_gate_eligible") is False
    ]
    retrieval_all = [
        result["retrieval"]
        for result in successful
        if isinstance(result.get("retrieval"), Mapping) and result["retrieval"].get("applicable") is not False
    ]
    retrieval = [
        result["retrieval"]
        for result in successful
        if isinstance(result.get("retrieval"), Mapping) and result["retrieval"].get("eligible")
    ]
    session_retrieval = [
        result["retrieval"]
        for result in successful
        if isinstance(result.get("retrieval"), Mapping) and result["retrieval"].get("session_eligible")
    ]
    gate_retrieval_all = [
        result["retrieval"]
        for result in gate_successful
        if isinstance(result.get("retrieval"), Mapping) and result["retrieval"].get("applicable") is not False
    ]
    gate_retrieval = [item for item in gate_retrieval_all if item.get("eligible")]
    gate_session_retrieval = [item for item in gate_retrieval_all if item.get("session_eligible")]
    qa = [
        result["qa"]
        for result in successful
        if isinstance(result.get("qa"), Mapping) and isinstance(result["qa"].get("correct"), bool)
    ]
    gate_qa = [
        result["qa"]
        for result in gate_successful
        if isinstance(result.get("qa"), Mapping) and isinstance(result["qa"].get("correct"), bool)
    ]

    def average(field: str, items: Sequence[Mapping[str, Any]]) -> float | None:
        values = [float(item[field]) for item in items if item.get(field) is not None]
        return mean(values) if values else None

    coverage_values = [
        (
            bool((result.get("retrieval") or {}).get("answer_covered_by_extracted_claims"))
            if isinstance(result.get("retrieval"), Mapping)
            else False
        )
        for result in successful
        if not isinstance(result.get("retrieval"), Mapping)
        or (
            result["retrieval"].get("applicable") is not False
            and result["retrieval"].get("extraction_applicable") is not False
        )
    ]
    summary: dict[str, Any] = {
        "cases": len(results),
        "successful_cases": len(successful),
        "failed_cases": len(results) - len(successful),
        "retrieval_reported_cases": len(retrieval_all),
        "retrieval_eligible_cases": len(retrieval),
        "retrieval_eligible_numerator": len(retrieval),
        "retrieval_eligible_denominator": len(successful),
        **{f"recall_at_{k}": average(f"recall_at_{k}", retrieval) for k in RETRIEVAL_KS},
        **{f"hit_rate_at_{k}": average(f"hit_at_{k}", retrieval) for k in RETRIEVAL_KS},
        "mrr": average("mrr", retrieval),
        "session_retrieval_eligible_cases": len(session_retrieval),
        "session_retrieval_eligible_numerator": len(session_retrieval),
        "session_retrieval_eligible_denominator": len(successful),
        **{f"session_recall_at_{k}": average(f"session_recall_at_{k}", session_retrieval) for k in RETRIEVAL_KS},
        **{f"session_hit_rate_at_{k}": average(f"session_hit_at_{k}", session_retrieval) for k in RETRIEVAL_KS},
        "session_mrr": average("session_mrr", session_retrieval),
        "extraction_coverage_numerator": sum(coverage_values),
        "extraction_coverage_denominator": len(coverage_values),
        "answer_covered_by_extracted_claims": mean(coverage_values) if coverage_values else None,
        "qa_evaluated_cases": len(qa),
        "qa_accuracy": mean(float(item["correct"]) for item in qa) if qa else None,
        "gate_eligible_cases": len(gate_eligible),
        "gate_excluded_cases": len(gate_excluded),
        "gate_excluded_case_ids": [str(item.get("case_id") or "") for item in gate_excluded],
        "gate_retrieval_reported_cases": len(gate_retrieval_all),
        "gate_retrieval_eligible_cases": len(gate_retrieval),
        **{f"gate_recall_at_{k}": average(f"recall_at_{k}", gate_retrieval) for k in RETRIEVAL_KS},
        **{f"gate_hit_rate_at_{k}": average(f"hit_at_{k}", gate_retrieval) for k in RETRIEVAL_KS},
        "gate_mrr": average("mrr", gate_retrieval),
        "gate_session_retrieval_eligible_cases": len(gate_session_retrieval),
        **{
            f"gate_session_recall_at_{k}": average(f"session_recall_at_{k}", gate_session_retrieval)
            for k in RETRIEVAL_KS
        },
        **{
            f"gate_session_hit_rate_at_{k}": average(f"session_hit_at_{k}", gate_session_retrieval)
            for k in RETRIEVAL_KS
        },
        "gate_session_mrr": average("session_mrr", gate_session_retrieval),
        "gate_qa_evaluated_cases": len(gate_qa),
        "gate_qa_accuracy": mean(float(item["correct"]) for item in gate_qa) if gate_qa else None,
    }
    for k in RETRIEVAL_KS:
        claim_values = [item.get(f"recall_at_{k}") for item in retrieval if item.get(f"recall_at_{k}") is not None]
        summary[f"recall_at_{k}_eligible_numerator"] = len(claim_values)
        summary[f"recall_at_{k}_eligible_denominator"] = len(successful)
        session_values = [
            item.get(f"session_recall_at_{k}")
            for item in session_retrieval
            if item.get(f"session_recall_at_{k}") is not None
        ]
        summary[f"session_recall_at_{k}_eligible_numerator"] = len(session_values)
        summary[f"session_recall_at_{k}_eligible_denominator"] = len(successful)
    return summary


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


def _file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_llm_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    return parsed._replace(
        scheme=parsed.scheme.casefold(),
        netloc=parsed.netloc.casefold(),
        path=parsed.path.rstrip("/"),
        fragment="",
    ).geturl()


def _llm_configuration_identity(settings: Settings) -> dict[str, Any]:
    component_settings = _component_llm_settings(settings)
    return {
        "extractor_model": settings.llm_model,
        "extractor_provider": settings.llm_provider,
        "extractor_effective_provider": component_settings.llm_provider,
        "extractor_base_url": _normalized_llm_base_url(settings.llm_base_url),
        "extractor_structured_mode": settings.llm_structured_mode,
        "extractor_thinking": settings.enable_llm_thinking,
        "query_expansion_model": settings.query_expansion_model or settings.llm_model,
    }


def _manifest_identity(
    case: LongMemEvalCase,
    settings: Settings,
    *,
    relevance_scorer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the fields that make an ingest/relevance cache reusable."""
    llm_identity = _llm_configuration_identity(settings)
    identity: dict[str, Any] = {
        "case_id": case.case_id,
        "case_fingerprint": _case_fingerprint(case),
        "session_count": len(case.sessions),
        "event_model_version": BENCHMARK_EVENT_MODEL_VERSION,
        "extractor_version": LLM_EXTRACTOR_VERSION,
        "extraction_fragment_protocol": EXTRACTION_FRAGMENT_PROTOCOL_VERSION,
        "extraction_chunk_target_chars": settings.extraction_chunk_target_chars,
        "extraction_chunk_overlap_turns": settings.extraction_chunk_overlap_turns,
        **{key: value for key, value in llm_identity.items() if key != "query_expansion_model"},
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


def _turn_content(session: SessionInput, turn_index: int, message: Mapping[str, str]) -> dict[str, Any]:
    role = _normalize_role(message.get("role"))
    return {
        "text": _normalize_content(message.get("content") or ""),
        "messages": [{"role": role, "content": _normalize_content(message.get("content") or "")}],
        "benchmark_locator": {
            "session_id": session.session_id,
            "turn_index": turn_index,
            "span": [turn_index, turn_index + 1],
            "source_role": role,
        },
    }


def _claim_inflation_diagnostics(connection: Any, stats: Mapping[str, Any]) -> dict[str, Any]:
    """Measure claim density and lexical adjacent-turn restatement candidates."""

    def ratio(numerator: int | None, denominator: int) -> float | None:
        return round(numerator / denominator, 6) if numerator is not None and denominator else None

    stored_row = connection.execute("SELECT COUNT(*) FROM claims").fetchone()
    stored = int(stored_row[0]) if stored_row is not None else 0

    rows = connection.execute(
        "SELECT c.id,c.subject_entity_id,c.canonical_attribute,c.index_text,c.status,"
        "e.session_id,e.content_json FROM claims c "
        "JOIN evidence_links l ON l.derived_type='claim' AND l.derived_id=c.id AND l.evidence_type='event' "
        "JOIN events e ON e.id=l.evidence_id "
        "WHERE c.status IN ('active','candidate','disputed') "
        "ORDER BY c.id,e.id"
    ).fetchall()
    claims: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            content = json.loads(row["content_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        locator = content.get("benchmark_locator") if isinstance(content, Mapping) else None
        turn_index = locator.get("turn_index") if isinstance(locator, Mapping) else None
        located_session = locator.get("session_id") if isinstance(locator, Mapping) else None
        session_id = str(located_session or row["session_id"] or "").strip()
        if not session_id or not isinstance(turn_index, int) or isinstance(turn_index, bool):
            continue
        claim = claims.setdefault(
            str(row["id"]),
            {
                "subject": str(row["subject_entity_id"] or ""),
                "attribute": str(row["canonical_attribute"] or ""),
                "status": str(row["status"] or ""),
                "text": _reader_match_text(row["index_text"] or ""),
                "locations": set(),
            },
        )
        claim["locations"].add((session_id, turn_index))

    adjacent_pairs: set[tuple[str, str]] = set()
    claims_by_location: dict[tuple[str, int], set[str]] = {}
    for claim_id, claim in claims.items():
        for location in claim["locations"]:
            claims_by_location.setdefault(location, set()).add(claim_id)
    for (session_id, turn_index), left_ids in claims_by_location.items():
        right_ids = claims_by_location.get((session_id, turn_index + 1), set())
        for left_id in left_ids:
            left = claims[left_id]
            if not left["text"]:
                continue
            for right_id in right_ids:
                if left_id == right_id:
                    continue
                right = claims[right_id]
                if (left["subject"], left["attribute"], left["status"]) != (
                    right["subject"],
                    right["attribute"],
                    right["status"],
                ):
                    continue
                if (
                    right["text"]
                    and SequenceMatcher(
                        None,
                        left["text"],
                        right["text"],
                        autojunk=False,
                    ).ratio()
                    >= CLAIM_RESTATEMENT_LEXICAL_THRESHOLD
                ):
                    adjacent_pairs.add((min(left_id, right_id), max(left_id, right_id)))

    events = int(stats.get("events", 0))
    sessions = int(stats.get("sessions", 0))
    raw_extracted = stats.get("extracted_claims")
    extracted = int(raw_extracted) if isinstance(raw_extracted, int) and not isinstance(raw_extracted, bool) else None
    return {
        "claim_inflation_diagnostics_status": "computed",
        "stored_claims": stored,
        "extracted_claims_per_event": ratio(extracted, events),
        "stored_claims_per_event": ratio(stored, events),
        "stored_claims_per_session": ratio(stored, sessions),
        "adjacent_restatement_candidates": len(adjacent_pairs),
        "adjacent_restatement_definition": (
            "same subject, canonical_attribute and non-terminal lifecycle status, adjacent session turns, "
            f"diagnostic lexical threshold >= {CLAIM_RESTATEMENT_LEXICAL_THRESHOLD:g}; "
            "not the production semantic/cosine dedup threshold"
        ),
    }


def _claim_diagnostic_defaults(status: str) -> dict[str, Any]:
    return {
        "claim_inflation_diagnostics_status": status,
        "stored_claims": None,
        "extracted_claims_per_event": None,
        "stored_claims_per_event": None,
        "stored_claims_per_session": None,
        "adjacent_restatement_candidates": None,
        "adjacent_restatement_definition": None,
    }


def _cached_ingest_diagnostics(
    connection: Any,
    case: LongMemEvalCase,
    manifest_reference: str,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "sessions": len(case.sessions),
        "events": sum(len(session.messages) for session in case.sessions),
        "extracted_claims": None,
    }
    diagnostics = _claim_inflation_diagnostics(connection, stats)
    diagnostics["claim_inflation_diagnostics_status"] = "computed_from_cache"
    return {
        "skipped": True,
        "cache_manifest": manifest_reference,
        **stats,
        **diagnostics,
    }


class _UnlimitedBenchmarkBudget:
    """Benchmark 统计真实 usage，但不把多个 case 绑定到在线日预算。"""

    def can_spend(self, _tokens: int) -> bool:
        return True

    def record_usage(self, _tokens: int) -> None:
        return None

    def get_stats(self) -> dict[str, int]:
        return {"used": 0, "limit": 0, "remaining": 0}


def _data_inspection_code(error: httpx.HTTPStatusError) -> str | None:
    """Return the provider code only for an explicit content-inspection rejection."""
    response = error.response
    if response is None or response.status_code != 400:
        return None
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("error")
    code = detail.get("code") if isinstance(detail, dict) else payload.get("code")
    normalized = str(code or "").strip().casefold()
    return normalized if normalized == "data_inspection_failed" else None


class _BenchmarkWorker(Worker):
    """Keep an evaluation case running when one source event is provider-rejected."""

    _SUM_FIELDS = (
        "events",
        "eligible_events",
        "claims",
        "stored",
        "skipped",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "content_inspection_skipped_events",
    )

    def _extract_window(self, event_ids: list[str], job_id: str | None) -> dict[str, Any]:
        try:
            return super()._extract_window(event_ids, job_id)
        except httpx.HTTPStatusError as error:
            code = _data_inspection_code(error)
            if code is None:
                raise
            extractor = getattr(self, "extractor", None)
            failed_usage = {
                "input_tokens": int(getattr(extractor, "last_input_tokens", 0)),
                "output_tokens": int(getattr(extractor, "last_output_tokens", 0)),
                "total_tokens": int(getattr(extractor, "last_usage_tokens", 0)),
            }
            if len(event_ids) == 1:
                return {
                    "events": 1,
                    "eligible_events": 1,
                    "claims": 0,
                    "stored": 0,
                    "skipped": 0,
                    "rejections": [],
                    **failed_usage,
                    "content_inspection_skipped_events": 1,
                    "content_inspection_codes": [code],
                }

            parts = [self._extract_window([event_id], job_id) for event_id in event_ids]
            merged: dict[str, Any] = {
                field: sum(int(part.get(field, 0)) for part in parts) for field in self._SUM_FIELDS
            }
            for field, value in failed_usage.items():
                merged[field] += value
            merged["rejections"] = [rejection for part in parts for rejection in list(part.get("rejections") or [])]
            merged["content_inspection_codes"] = list(
                dict.fromkeys(
                    code_value for part in parts for code_value in list(part.get("content_inspection_codes") or [])
                )
            )
            return merged


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
    extractor = make_extractor(
        _component_llm_settings(settings),
        require_real=True,
        connection=connection,
    )
    stats: dict[str, Any] = {
        "sessions": len(case.sessions),
        "events": sum(len(session.messages) for session in case.sessions),
        "extracted_claims": 0,
        "accepted_claim_writes": 0,
        "skipped_claims": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "content_inspection_skipped_events": 0,
        "content_inspection_codes": [],
    }
    started = time.perf_counter()
    for index, session in enumerate(case.sessions, start=1):
        for turn_index, message in enumerate(session.messages):
            role = _normalize_role(message.get("role"))
            content = _turn_content(session, turn_index, message)
            event = {
                "id": _turn_event_id(session.event_id, turn_index),
                "idempotency_key": f"longmemeval:{case.case_id}:{session.session_id}:turn:{turn_index}",
                "tenant_id": case.namespace,
                "session_id": session.session_id,
                "event_type": "message",
                "actor_type": role,
                "content": content,
                "metadata": {"turn_index": turn_index},
                "occurred_at": session.occurred_at,
                "source_uri": f"longmemeval:{case.case_id}:{session.session_id}:turn:{turn_index}",
            }
            service.ingest_event(event)
        if index == 1 or index % 10 == 0 or index == len(case.sessions):
            print(
                f"[{case_number}/{total_hint}] {case.case_id}: queued {index}/{len(case.sessions)} sessions",
                flush=True,
            )
    worker = _BenchmarkWorker(
        settings,
        connection=connection,
        extractor=extractor,
        embedder=embedder,
        image_describer=None,
        budget=_UnlimitedBenchmarkBudget(),
        audit_logger=NullAuditLogger(),
    )
    try:
        while True:
            result = worker.run_once(force_extraction=True)
            if result["status"] == "idle":
                break
            if result["status"] != "succeeded":
                raise RuntimeError(
                    f"production extraction worker failed for {case.case_id}: "
                    f"{result.get('error') or result['status']}"
                )
            if "events" not in result:
                continue
            stats["extracted_claims"] += int(result.get("claims", 0))
            stats["accepted_claim_writes"] += int(result.get("stored", 0))
            stats["skipped_claims"] += int(result.get("skipped", 0))
            stats["input_tokens"] += int(result.get("input_tokens", 0))
            stats["output_tokens"] += int(result.get("output_tokens", 0))
            stats["total_tokens"] += int(result.get("total_tokens", 0))
            stats["content_inspection_skipped_events"] += int(result.get("content_inspection_skipped_events", 0))
            stats["content_inspection_codes"] = list(
                dict.fromkeys(
                    [
                        *stats["content_inspection_codes"],
                        *list(result.get("content_inspection_codes") or []),
                    ]
                )
            )
    finally:
        worker.close()
    stats.update(_claim_inflation_diagnostics(connection, stats))
    stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return stats


def _run_case_maintenance(connection: Any, settings: Settings) -> dict[str, Any]:
    """Run the lightweight deterministic subset of production maintenance."""
    dedup = (
        review_pending_near_duplicates(
            connection,
            threshold=settings.dedup_threshold,
            limit=settings.dedup_scan_limit,
        )
        if settings.dedup_enabled
        else {"scanned": 0, "equivalent": 0, "deferred": 0, "missing": 0}
    )
    maintenance_now = datetime.now(timezone.utc).isoformat()
    conflicts = auto_resolve_conflicts(connection, maintenance_now)
    return {
        "protocol": BENCHMARK_MAINTENANCE_PROTOCOL_VERSION,
        "dedup_enabled": settings.dedup_enabled,
        "dedup": dedup,
        "conflicts": conflicts,
        "completed_at": maintenance_now,
    }


def _retrieved_payload(
    results: Sequence[Mapping[str, Any]],
    case: LongMemEvalCase,
    *,
    search_trace: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    event_to_session = _event_to_session(case)
    raw_candidates = search_trace.get("candidates") if search_trace is not None else None
    trace_candidates = raw_candidates if isinstance(raw_candidates, Mapping) else {}
    payload: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        evidence_ids = _result_evidence_ids(result)
        claim_id = str(result.get("id"))
        raw_candidate = trace_candidates.get(claim_id)
        candidate = raw_candidate if isinstance(raw_candidate, Mapping) else {}
        raw_channels = candidate.get("channels")
        channel_ranks = dict(raw_channels) if isinstance(raw_channels, Mapping) else {}
        raw_channel_scores = candidate.get("channel_scores")
        channel_scores = dict(raw_channel_scores) if isinstance(raw_channel_scores, Mapping) else {}
        dense_scores = [
            float(score)
            for channel, score in channel_scores.items()
            if (channel == "dense" or str(channel).endswith(":dense")) and isinstance(score, (int, float))
        ]
        raw_features = result.get("features")
        features = dict(raw_features) if isinstance(raw_features, Mapping) else {}
        raw_filter_reasons = candidate.get("filter_reasons")
        filter_reasons = list(raw_filter_reasons) if isinstance(raw_filter_reasons, list) else []
        payload.append(
            {
                "rank": rank,
                "final_rank": rank,
                "recall_final_rank": candidate.get("final_rank"),
                "claim_id": result.get("id"),
                "text": result.get("text"),
                "value": result.get("value"),
                "score": result.get("score"),
                "score_path": result.get("score_path"),
                "dense_score": max(dense_scores) if dense_scores else None,
                "reranker_raw_score": result.get("reranker_raw_score", candidate.get("rerank_score")),
                "pre_rank": candidate.get("pre_rank"),
                "pre_score": candidate.get("pre_score"),
                "reranker_rank": candidate.get("rerank_rank"),
                "features": features,
                "channel_ranks": channel_ranks,
                "channel_scores": channel_scores,
                "filter_reasons": filter_reasons,
                "status": result.get("status"),
                "valid_from": result.get("valid_from"),
                "valid_to": result.get("valid_to"),
                "recorded_from": result.get("recorded_from"),
                "recorded_to": result.get("recorded_to"),
                "occurred_start": result.get("occurred_start"),
                "occurred_end": result.get("occurred_end"),
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
        make_query_expander(_component_llm_settings(settings), connection),
    )
    started = time.perf_counter()
    response = service.recall(
        case.question,
        limit=_reader_recall_limit(case),
        as_of=case.question_at,
        intent=_longmemeval_recall_intent(case),
        namespace=case.namespace,
        debug=True,
        ranking_now=case.question_at,
    )
    raw_results = response.get("results") or []
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)]
    claim_values = _claim_values(connection)
    for result in results:
        claim_id = str(result.get("id"))
        result["value"] = claim_values.get(claim_id)
    results = _production_order_top_k(results, READER_EVIDENCE_LIMIT)
    if relevance_by_claim_id is None and case.answer.strip():
        scorer = relevance_embedder or embedder
        relevance_by_claim_id = _claim_relevance_scores(claim_values, case.answer, scorer)
    metrics = retrieval_metrics(
        results,
        case.gold_event_ids,
        relevance_by_claim_id=relevance_by_claim_id if case.answer.strip() else None,
        gold_session_ids=case.gold_session_ids,
        event_to_session=_event_to_session(case),
    )
    metrics.update(
        retrieved_claims=len(results),
        elapsed_seconds=round(time.perf_counter() - started, 3),
        search_trace=response.get("search_trace"),
    )
    search_trace = response.get("search_trace")
    return metrics, _retrieved_payload(
        results,
        case,
        search_trace=search_trace if isinstance(search_trace, Mapping) else None,
    )


def _find_qa_timeout(error: BaseException) -> httpx.ReadTimeout | httpx.ConnectTimeout | None:
    return cast(
        httpx.ReadTimeout | httpx.ConnectTimeout | None,
        find_http_exception(error, (httpx.ReadTimeout, httpx.ConnectTimeout)),
    )


def _qa_call_with_retry(
    call: Callable[[], _T],
    *,
    max_attempts: int = QA_MAX_ATTEMPTS,
) -> _T:
    """Retry one reader/judge call through the shared HTTP policy."""
    return cast(_T, qa_call_with_retry(call, max_attempts=max_attempts, sleep=time.sleep))


def _case_error_type(error: BaseException) -> str:
    http_error = _find_http_status_error(error)
    if http_error is None:
        return type(error).__name__
    status = http_error.response.status_code
    if status in {401, 403}:
        return f"http_{status}"
    if status == 429:
        try:
            response_text = http_error.response.text.lower()
        except httpx.ResponseNotRead:
            response_text = ""
        return "quota" if any(token in response_text for token in ("quota", "arrearage", "balance")) else "http_429"
    if status >= 500:
        return "http_5xx"
    return f"http_{status}"


def _http_diagnostic_secrets(settings: Settings) -> tuple[str, ...]:
    values = (
        settings.llm_api_key,
        settings.embedding_api_key,
        settings.reranker_api_key,
        settings.image_describer_api_key,
        os.environ.get("LLM_API_KEY"),
        os.environ.get("EMBEDDING_API_KEY"),
        os.environ.get("RERANKER_API_KEY"),
        os.environ.get("IMAGE_API_KEY"),
    )
    return tuple(dict.fromkeys(value for value in values if value))


def _result_error_type(result: Mapping[str, Any]) -> str | None:
    if not result.get("error"):
        return None
    error_type = str(result.get("error_type") or "").strip()
    if error_type:
        return error_type
    return str(result["error"]).partition(":")[0] or "unknown_error"


def _qa_model(settings: Settings) -> str:
    return str(qa_model(settings.llm_model))


def _reader_generation_options(model: str) -> dict[str, int]:
    """Bound reader reasoning while preserving a short final-answer allowance."""
    folded = model.casefold()
    options = {"max_tokens": READER_ANSWER_TOKEN_BUDGET}
    if folded.startswith(("qwen3.7-", "deepseek-v4-")):
        options["thinking_budget"] = READER_THINKING_TOKEN_BUDGET
    if folded.startswith("deepseek-v4-"):
        options["max_tokens"] += READER_THINKING_TOKEN_BUDGET
    return options


def _component_llm_settings(settings: Settings) -> Settings:
    """Select Bailian's payload dialect without mutating reported configuration."""
    parsed = urlsplit(settings.llm_base_url)
    hostname = (parsed.hostname or "").casefold()
    is_bailian_compatible = parsed.path.rstrip("/").endswith("/compatible-mode/v1") and (
        hostname == "dashscope.aliyuncs.com"
        or (hostname.startswith("dashscope-") and hostname.endswith(".aliyuncs.com"))
        or hostname.endswith(".maas.aliyuncs.com")
    )
    if settings.llm_provider == "openai_compatible" and is_bailian_compatible:
        return dataclasses.replace(settings, llm_provider="dashscope")
    return settings


def _judge_longmemeval_answer(
    *,
    api_key: str,
    base_url: str,
    model: str,
    case_id: str,
    question_type: str,
    question: str,
    answer: str,
    predicted_answer: str,
    usage_details: list[QAUsage] | None = None,
) -> tuple[dict[str, Any], int]:
    def judge_chat(
        chat_api_key: str,
        chat_base_url: str,
        chat_model: str,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.1,
    ) -> tuple[str, int]:
        if usage_details is not None:
            text, usage = _qa_dashscope_chat_detailed(
                chat_api_key,
                chat_base_url,
                chat_model,
                system_prompt,
                user_prompt,
                temperature=temperature,
                enable_thinking=False,
                json_object=True,
            )
            usage_details.append(usage)
            return text, usage.total_tokens
        return cast(
            tuple[str, int],
            _qa_dashscope_chat(
                chat_api_key,
                chat_base_url,
                chat_model,
                system_prompt,
                user_prompt,
                temperature=temperature,
                enable_thinking=False,
                json_object=True,
            ),
        )

    return cast(
        tuple[dict[str, Any], int],
        _judge_longmemeval_answer_impl(
            api_key=api_key,
            base_url=base_url,
            model=model,
            case_id=case_id,
            question_type=question_type,
            question=question,
            answer=answer,
            predicted_answer=predicted_answer,
            call_with_retry=_qa_call_with_retry,
            chat=judge_chat,
            decode_response=_response_object,
        ),
    )


def _is_preference_recommendation(case: LongMemEvalCase) -> bool:
    return "preference" in case.question_type.casefold() and bool(_RECOMMENDATION_QUESTION_RE.search(case.question))


def _is_count_or_sum_question(case: LongMemEvalCase) -> bool:
    return bool(_COUNT_OR_SUM_QUESTION_RE.search(case.question))


def _reader_system_prompt(case: LongMemEvalCase) -> str:
    prompt = (
        "You answer questions from retrieved long-term-memory claims and their original evidence events. "
        "Before answering, perform a private Chain-of-Note pass over every relevant record: (1) note each candidate "
        "answer and its exact relation to the question; (2) label whether it was planned or intended, attempted, "
        "or actually executed; (3) use occurred and valid times plus Current Date to resolve updates; "
        "and (4) compare the candidates and synthesize only the one whose relation and state answer the question. "
        "Do not expose these private notes. Keep audition distinct from participation in a production; keep location, "
        "travel duration, and distance distinct; and never treat a plan as completed execution. Related distractors "
        "must not override evidence with the exact requested relation. Combine records when needed and allow only "
        "deterministic coreference resolution, simple arithmetic, comparisons, and date calculations. Do not invent "
        "missing proper nouns, amounts, places, dates, or counts. Say that the information is unavailable only after "
        "checking every claim and evidence event and finding them genuinely insufficient; do not abstain merely "
        "because the wording differs. Count repeated phrasings of the same fact only once and prefer the most complete "
        "version; never automatically merge records whose number, date, weekday, entity, or qualifier differs. Return "
        "only the final answer, without analysis, private notes, or evidence ranks."
    )
    if _is_preference_recommendation(case):
        prompt += (
            " For this preference recommendation, treat the memories as constraints for generation, not as a closed "
            "catalog of answer strings. You may synthesize a recommendation that satisfies those constraints even when "
            "the specific proper noun is absent from memory. The final answer must explicitly use the known preferences "
            "or experiences that justify the recommendation; if no relevant personal constraint is present, say the "
            "information is unavailable instead of giving an ungrounded generic recommendation."
        )
    if _is_count_or_sum_question(case):
        prompt += (
            " For count or sum questions, enumerate every record you can see, cautiously deduplicate identical items "
            "so each item is counted once, and only then compute the total."
        )
    if "knowledge-update" in case.question_type.casefold():
        prompt += (
            " For knowledge-update questions, prefer the latest statement that is valid at the question time; older "
            "conflicting statements are history only and must never override the updated value."
        )
    if "temporal" in case.question_type.casefold():
        prompt += (
            " For temporal questions, first select the latest baseline effective at the question time, then apply "
            "weekday conditions or relative offsets to that baseline; never apply an offset to a superseded baseline. "
            "For a historical question, select the baseline that was effective at that historical time and never import "
            "a later current value."
        )
    return prompt


def _run_qa(
    connection: Any,
    case: LongMemEvalCase,
    retrieved: Sequence[Mapping[str, Any]],
    settings: Settings,
    *,
    reader_context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> dict[str, Any]:
    qa_model = _qa_model(settings)
    api_key = os.environ.get("LLM_API_KEY") or settings.llm_api_key
    if not api_key:
        raise RuntimeError("QA answering requires LLM_API_KEY in .env or environment")

    reader_system_prompt = _reader_system_prompt(case)
    reader_user_prompt = _build_reader_user_prompt(
        connection, cast(Any, case), retrieved, context_mode=reader_context_mode
    )
    reader_generation = _reader_generation_options(qa_model)
    reader_text, reader_tokens = _qa_call_with_retry(
        lambda: _qa_dashscope_chat(
            api_key,
            settings.llm_base_url,
            qa_model,
            reader_system_prompt,
            reader_user_prompt,
            enable_thinking=True,
            thinking_budget=reader_generation.get("thinking_budget"),
            max_tokens=reader_generation["max_tokens"],
        )
    )
    predicted = reader_text.strip()

    judgment, judge_tokens = _judge_longmemeval_answer(
        api_key=str(api_key),
        base_url=settings.llm_base_url,
        model=qa_model,
        case_id=case.case_id,
        question_type=case.question_type,
        question=case.question,
        answer=case.answer,
        predicted_answer=predicted,
    )
    return {
        "model": qa_model,
        "predicted_answer": predicted,
        "correct": judgment["correct"],
        "reason": str(judgment.get("reason") or ""),
        "usage": {
            "reader_tokens": reader_tokens,
            "judge_tokens": judge_tokens,
            "total_tokens": reader_tokens + judge_tokens,
        },
    }


def _full_context_reader_system_prompt(case: LongMemEvalCase) -> str:
    """Reuse the benchmark reader rules while naming the control's real input."""
    return _reader_system_prompt(case).replace(
        "retrieved long-term-memory claims and their original evidence events",
        "the complete timestamped chat history",
        1,
    )


def _usage_fields(reader: QAUsage, judge: QAUsage) -> dict[str, int]:
    return {
        "reader_tokens": reader.total_tokens,
        "judge_tokens": judge.total_tokens,
        "total_tokens": reader.total_tokens + judge.total_tokens,
        "reader_input_tokens": reader.input_tokens,
        "reader_output_tokens": reader.output_tokens,
        "reader_reasoning_tokens": reader.reasoning_tokens,
        "reader_answer_tokens": reader.answer_tokens,
        "judge_input_tokens": judge.input_tokens,
        "judge_output_tokens": judge.output_tokens,
        "judge_reasoning_tokens": judge.reasoning_tokens,
        "judge_answer_tokens": judge.answer_tokens,
        "total_input_tokens": reader.input_tokens + judge.input_tokens,
        "total_output_tokens": reader.output_tokens + judge.output_tokens,
        "total_reasoning_tokens": reader.reasoning_tokens + judge.reasoning_tokens,
        "total_answer_tokens": reader.answer_tokens + judge.answer_tokens,
    }


def _priced_request_cost(usage: QAUsage, input_rate: float, output_rate: float) -> float:
    return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) / 1_000_000


def _full_context_cost(model: str, reader: QAUsage, judge: QAUsage) -> dict[str, Any]:
    """Estimate control cost only when this runner has an explicit model rate."""
    if not model.casefold().startswith("deepseek-v4-flash"):
        return {
            "currency": "CNY",
            "priced": False,
            "input_cny_per_million": None,
            "output_cny_per_million": None,
            "reader_cny": None,
            "judge_cny": None,
            "total_cny": None,
            "reason": "no pinned evaluation rate for this model override",
        }
    input_rate = _DEEPSEEK_V4_FLASH_INPUT_CNY_PER_MILLION
    output_rate = _DEEPSEEK_V4_FLASH_OUTPUT_CNY_PER_MILLION
    reader_cost = _priced_request_cost(reader, input_rate, output_rate)
    judge_cost = _priced_request_cost(judge, input_rate, output_rate)
    return {
        "currency": "CNY",
        "priced": True,
        "input_cny_per_million": input_rate,
        "output_cny_per_million": output_rate,
        "rate_basis": "Bailian deepseek-v4-flash model pricing snapshot",
        "rate_snapshot_date": "2026-08-12",
        "reader_cny": round(reader_cost, 6),
        "judge_cny": round(judge_cost, 6),
        "total_cny": round(reader_cost + judge_cost, 6),
    }


def _run_full_context_case(case: LongMemEvalCase, settings: Settings) -> dict[str, Any]:
    """Answer one case from every raw session without creating or querying a memory DB."""
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "question_type": case.question_type,
        "question": case.question,
        "answer": case.answer,
        "session_count": len(case.sessions),
        "gold_session_ids": list(case.gold_session_ids),
        "evaluation_eligibility": _evaluation_eligibility(case),
        "control": "full-context",
        "database": None,
        "ingest": None,
        "maintenance": None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "cost": None,
        "error": None,
        "error_type": None,
        "error_diagnostics": None,
    }
    started = time.perf_counter()
    try:
        api_key = os.environ.get("LLM_API_KEY") or settings.llm_api_key
        if not api_key:
            raise RuntimeError("full-context reader requires LLM_API_KEY in .env or environment")
        model = _qa_model(settings)
        rendered = render_full_context_user_prompt(case)
        selected_ids = set(rendered.selected_session_ids)
        gold_ids = set(case.gold_session_ids)
        result["retrieval"] = {
            "applicable": False,
            "selector": "all-sessions",
            "query": case.question,
            "sessions_considered": len(case.sessions),
            "sessions_selected": rendered.session_count,
            "messages_selected": rendered.message_count,
            "all_sessions_selected": rendered.session_count == len(case.sessions),
            "selected_session_ids": list(rendered.selected_session_ids),
            "gold_session_ids_present": sorted(gold_ids & selected_ids),
            "gold_session_coverage": len(gold_ids & selected_ids) / len(gold_ids) if gold_ids else None,
            "context_chars": rendered.context_chars,
            "reader_prompt_chars": rendered.prompt_chars,
            "truncated": False,
        }
        generation = _reader_generation_options(model)
        reader_started = time.perf_counter()
        reader_text, reader_usage = _qa_call_with_retry(
            lambda: _qa_dashscope_chat_detailed(
                str(api_key),
                settings.llm_base_url,
                model,
                _full_context_reader_system_prompt(case),
                rendered.prompt,
                enable_thinking=True,
                thinking_budget=generation.get("thinking_budget"),
                max_tokens=generation["max_tokens"],
                timeout_seconds=FULL_CONTEXT_READER_TIMEOUT_SECONDS,
            )
        )
        reader_elapsed = time.perf_counter() - reader_started
        predicted = reader_text.strip()
        judge_usages: list[QAUsage] = []
        judge_started = time.perf_counter()
        judgment, judge_tokens = _judge_longmemeval_answer(
            api_key=str(api_key),
            base_url=settings.llm_base_url,
            model=model,
            case_id=case.case_id,
            question_type=case.question_type,
            question=case.question,
            answer=case.answer,
            predicted_answer=predicted,
            usage_details=judge_usages,
        )
        judge_elapsed = time.perf_counter() - judge_started
        if len(judge_usages) != 1:
            raise RuntimeError("full-context judge did not return detailed token usage")
        judge_usage = judge_usages[0]
        if judge_usage.total_tokens != judge_tokens:
            raise RuntimeError("full-context judge token totals are inconsistent")
        result["qa"] = {
            "model": model,
            "predicted_answer": predicted,
            "correct": judgment["correct"],
            "reason": str(judgment.get("reason") or ""),
            "usage": _usage_fields(reader_usage, judge_usage),
            "latency_seconds": {
                "reader": round(reader_elapsed, 3),
                "judge": round(judge_elapsed, 3),
                "total": round(reader_elapsed + judge_elapsed, 3),
            },
        }
        result["cost"] = _full_context_cost(model, reader_usage, judge_usage)
    except Exception as error:
        diagnostic_secrets = _http_diagnostic_secrets(settings)
        result["error"] = sanitize_diagnostic_text(
            f"{type(error).__name__}: {error}",
            secrets=diagnostic_secrets,
        )
        result["error_type"] = _case_error_type(error)
        result["error_diagnostics"] = _evaluation_http_error_diagnostics(
            error,
            secrets=diagnostic_secrets,
        )
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def _native_rag_reader_system_prompt(case: LongMemEvalCase) -> str:
    """Reuse the benchmark reader rules while naming the dense RAG input."""
    return _reader_system_prompt(case).replace(
        "retrieved long-term-memory claims and their original evidence events",
        "timestamped raw chat sessions selected by dense retrieval",
        1,
    )


def _native_rag_embedding_config(settings: Settings) -> EmbeddingConfig:
    return EmbeddingConfig(
        code="NATIVE_RAG_V1",
        model=settings.embedding_model,
        api_kind="native",
        dim=settings.embedding_dim,
        batch_size=10,
    )


def _native_rag_cost(
    model: str,
    document_embedding: Cost,
    query_embedding: Cost,
    reader: QAUsage,
    judge: QAUsage,
) -> dict[str, Any]:
    llm_cost = _full_context_cost(model, reader, judge)
    index_embedding_cost = document_embedding.tokens * _QWEN37_EMBEDDING_INPUT_CNY_PER_MILLION / 1_000_000
    query_embedding_cost = query_embedding.tokens * _QWEN37_EMBEDDING_INPUT_CNY_PER_MILLION / 1_000_000
    embedding_cost = index_embedding_cost + query_embedding_cost
    llm_total = llm_cost.get("total_cny")
    online_cost = query_embedding_cost + float(llm_total) if llm_total is not None else None
    total_cost = index_embedding_cost + online_cost if online_cost is not None else None
    return {
        "currency": "CNY",
        "priced": llm_cost.get("priced") is True,
        "embedding_input_cny_per_million": _QWEN37_EMBEDDING_INPUT_CNY_PER_MILLION,
        "reader_input_cny_per_million": llm_cost.get("input_cny_per_million"),
        "reader_output_cny_per_million": llm_cost.get("output_cny_per_million"),
        "rate_basis": "Bailian model pricing snapshot",
        "rate_snapshot_date": "2026-08-12",
        "index_embedding_cny": round(index_embedding_cost, 6),
        "query_embedding_cny": round(query_embedding_cost, 6),
        "embedding_cny": round(embedding_cost, 6),
        "reader_cny": llm_cost.get("reader_cny"),
        "judge_cny": llm_cost.get("judge_cny"),
        "online_query_cny": round(online_cost, 6) if online_cost is not None else None,
        "cold_start_total_cny": round(total_cost, 6) if total_cost is not None else None,
        "total_cny": round(total_cost, 6) if total_cost is not None else None,
        "reason": llm_cost.get("reason"),
    }


def _run_native_rag_case(
    case: LongMemEvalCase,
    settings: Settings,
    embedding_client: DashScopeEmbeddingClient,
    embedding_config: EmbeddingConfig,
) -> dict[str, Any]:
    """Answer one case from exact-cosine Top-10 complete raw sessions."""
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "question_type": case.question_type,
        "question": case.question,
        "answer": case.answer,
        "session_count": len(case.sessions),
        "gold_session_ids": list(case.gold_session_ids),
        "evaluation_eligibility": _evaluation_eligibility(case),
        "control": "native-rag",
        "database": None,
        "ingest": None,
        "maintenance": None,
        "embedding": None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "cost": None,
        "error": None,
        "error_type": None,
        "error_diagnostics": None,
    }
    started = time.perf_counter()
    try:
        llm_api_key = os.environ.get("LLM_API_KEY") or settings.llm_api_key
        if not llm_api_key:
            raise RuntimeError("native-rag reader requires LLM_API_KEY in .env or environment")
        documents = render_raw_session_documents(case)
        cache_dir = NATIVE_RAG_CACHE_ROOT / _safe_case_name(case.case_id)

        document_started = time.perf_counter()
        document_output = embed_remote(
            embedding_client,
            embedding_config,
            "document",
            [document.text for document in documents],
            cache_dir=cache_dir,
            use_cache=True,
        )
        document_elapsed = time.perf_counter() - document_started
        query_started = time.perf_counter()
        query_output = embed_remote(
            embedding_client,
            embedding_config,
            "query",
            [case.question],
            cache_dir=cache_dir,
            use_cache=True,
        )
        query_elapsed = time.perf_counter() - query_started
        embedding_cost = Cost()
        embedding_cost.add(document_output.cost)
        embedding_cost.add(query_output.cost)
        hits = select_raw_sessions(
            documents,
            document_output.dense,
            query_output.dense[0],
            top_k=NATIVE_RAG_TOP_K,
        )
        rendered = render_native_rag_user_prompt(case, hits)
        reader_ranks = {session_id: rank for rank, session_id in enumerate(rendered.reader_session_ids, start=1)}
        gold_ids = set(case.gold_session_ids)
        retrieved = [
            {
                "id": hit.document.session_id,
                "session_id": hit.document.session_id,
                "occurred_at": hit.document.occurred_at,
                "source_index": hit.document.source_index,
                "message_count": hit.document.message_count,
                "text_chars": len(hit.document.text),
                "dense_score": hit.score,
                "final_score": hit.score,
                "retrieval_rank": hit.retrieval_rank,
                "reader_rank": reader_ranks[hit.document.session_id],
                "gold_session": hit.document.session_id in gold_ids,
                "evidence": [hit.document.session_id],
            }
            for hit in hits
        ]
        session_metrics = _session_retrieval_metrics(retrieved, case.gold_session_ids)
        selected_ids = set(rendered.retrieval_session_ids)
        result["embedding"] = {
            "model": embedding_config.model,
            "dimension": embedding_config.dim,
            "api_mode": embedding_config.api_kind,
            "text_type": None,
            "query_instruct": None,
            "documents": len(documents),
            "queries": 1,
            "usage": {
                **embedding_cost.as_dict(),
                "document": document_output.cost.as_dict(),
                "query": query_output.cost.as_dict(),
            },
            "latency_seconds": {
                "document_wall": round(document_elapsed, 3),
                "query_wall": round(query_elapsed, 3),
                "total_wall": round(document_elapsed + query_elapsed, 3),
                "provider_cold_start_recorded": round(embedding_cost.latency_seconds, 3),
            },
        }
        result["retrieved"] = retrieved
        result["retrieval"] = {
            "applicable": True,
            "extraction_applicable": False,
            "eligible": False,
            "selector": f"exact-cosine-top-{NATIVE_RAG_TOP_K}",
            "query": case.question,
            "top_k": NATIVE_RAG_TOP_K,
            "sessions_considered": len(documents),
            "sessions_selected": len(hits),
            "messages_selected": rendered.message_count,
            "selected_session_ids": list(rendered.retrieval_session_ids),
            "reader_session_ids": list(rendered.reader_session_ids),
            "gold_session_ids_present": sorted(gold_ids & selected_ids),
            "gold_session_coverage": len(gold_ids & selected_ids) / len(gold_ids) if gold_ids else None,
            "context_chars": rendered.context_chars,
            "reader_prompt_chars": rendered.prompt_chars,
            "truncated": False,
            "session_eligible": session_metrics["eligible"],
            **{f"session_recall_at_{k}": session_metrics[f"recall_at_{k}"] for k in RETRIEVAL_KS},
            **{f"session_hit_at_{k}": session_metrics[f"hit_at_{k}"] for k in RETRIEVAL_KS},
            "session_mrr": session_metrics["mrr"],
            "session_first_relevant_rank": session_metrics["first_relevant_rank"],
        }

        model = _qa_model(settings)
        generation = _reader_generation_options(model)
        reader_started = time.perf_counter()
        reader_text, reader_usage = _qa_call_with_retry(
            lambda: _qa_dashscope_chat_detailed(
                str(llm_api_key),
                settings.llm_base_url,
                model,
                _native_rag_reader_system_prompt(case),
                rendered.prompt,
                enable_thinking=True,
                thinking_budget=generation.get("thinking_budget"),
                max_tokens=generation["max_tokens"],
                timeout_seconds=FULL_CONTEXT_READER_TIMEOUT_SECONDS,
            )
        )
        reader_elapsed = time.perf_counter() - reader_started
        predicted = reader_text.strip()
        judge_usages: list[QAUsage] = []
        judge_started = time.perf_counter()
        judgment, judge_tokens = _judge_longmemeval_answer(
            api_key=str(llm_api_key),
            base_url=settings.llm_base_url,
            model=model,
            case_id=case.case_id,
            question_type=case.question_type,
            question=case.question,
            answer=case.answer,
            predicted_answer=predicted,
            usage_details=judge_usages,
        )
        judge_elapsed = time.perf_counter() - judge_started
        if len(judge_usages) != 1:
            raise RuntimeError("native-rag judge did not return detailed token usage")
        judge_usage = judge_usages[0]
        if judge_usage.total_tokens != judge_tokens:
            raise RuntimeError("native-rag judge token totals are inconsistent")
        result["qa"] = {
            "model": model,
            "predicted_answer": predicted,
            "correct": judgment["correct"],
            "reason": str(judgment.get("reason") or ""),
            "usage": _usage_fields(reader_usage, judge_usage),
            "latency_seconds": {
                "reader": round(reader_elapsed, 3),
                "judge": round(judge_elapsed, 3),
                "total": round(reader_elapsed + judge_elapsed, 3),
            },
        }
        result["cost"] = _native_rag_cost(
            model,
            document_output.cost,
            query_output.cost,
            reader_usage,
            judge_usage,
        )
    except Exception as error:
        diagnostic_secrets = _http_diagnostic_secrets(settings)
        result["error"] = sanitize_diagnostic_text(
            f"{type(error).__name__}: {error}",
            secrets=diagnostic_secrets,
        )
        result["error_type"] = _case_error_type(error)
        result["error_diagnostics"] = _evaluation_http_error_diagnostics(
            error,
            secrets=diagnostic_secrets,
        )
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


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
    reader_context_mode: str = DEFAULT_READER_CONTEXT_MODE,
) -> dict[str, Any]:
    database_path, manifest_path = _case_paths(case.case_id)
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "question_type": case.question_type,
        "question": case.question,
        "answer": case.answer,
        "session_count": len(case.sessions),
        "gold_session_ids": list(case.gold_session_ids),
        "evaluation_eligibility": _evaluation_eligibility(case),
        "database": str(database_path.relative_to(ROOT)),
        "ingest": None,
        "maintenance": None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "error": None,
        "error_type": None,
        "error_diagnostics": None,
    }
    database: Database | None = None
    started = time.perf_counter()
    try:
        DATABASE_ROOT.mkdir(parents=True, exist_ok=True)
        if skip_ingest:
            if not database_path.is_file():
                raise FileNotFoundError(f"--skip-ingest requires cached database: {database_path}")
            _validate_manifest(manifest_path, case, settings)
            result["ingest"] = {
                "skipped": True,
                "cache_manifest": str(manifest_path.relative_to(ROOT)),
                **_claim_diagnostic_defaults("unavailable_cache_open_failed"),
            }
        else:
            _remove_case_artifacts(database_path, manifest_path)

        database = Database(database_path, settings=settings)
        connection = database.open()
        if skip_ingest:
            result["ingest"] = _cached_ingest_diagnostics(
                connection,
                case,
                str(manifest_path.relative_to(ROOT)),
            )
        else:
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
        result["maintenance"] = _run_case_maintenance(connection, settings)
        result["retrieval"], result["retrieved"] = _recall_case(
            connection,
            case,
            settings,
            embedder,
            reranker,
        )
        if run_qa:
            result["qa"] = _run_qa(
                connection,
                case,
                result["retrieved"],
                settings,
                reader_context_mode=reader_context_mode,
            )
    except Exception as error:
        diagnostic_secrets = _http_diagnostic_secrets(settings)
        result["error"] = sanitize_diagnostic_text(
            f"{type(error).__name__}: {error}",
            secrets=diagnostic_secrets,
        )
        result["error_type"] = _case_error_type(error)
        result["error_diagnostics"] = _evaluation_http_error_diagnostics(
            error,
            secrets=diagnostic_secrets,
        )
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
        "evaluation_eligibility": _evaluation_eligibility(case),
        "database": str(variant_path.relative_to(ROOT)),
        "reembedding": None,
        "retrieval": None,
        "retrieved": [],
        "qa": None,
        "error": None,
        "error_type": None,
        "error_diagnostics": None,
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
        diagnostic_secrets = _http_diagnostic_secrets(settings)
        result["error"] = sanitize_diagnostic_text(
            f"{type(error).__name__}: {error}",
            secrets=diagnostic_secrets,
        )
        result["error_type"] = _case_error_type(error)
        result["error_diagnostics"] = _evaluation_http_error_diagnostics(
            error,
            secrets=diagnostic_secrets,
        )
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
            "event_model_version": BENCHMARK_EVENT_MODEL_VERSION,
            "extraction_fragment_protocol": EXTRACTION_FRAGMENT_PROTOCOL_VERSION,
            "extraction_chunk_target_chars": settings.extraction_chunk_target_chars,
            "extraction_chunk_overlap_turns": settings.extraction_chunk_overlap_turns,
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
    abort_reason: str | None = None,
) -> dict[str, Any]:
    llm_identity = _llm_configuration_identity(settings)
    report: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(args.dataset.resolve()),
            "bytes": args.dataset.stat().st_size if args.dataset.is_file() else None,
            "complete_json_array": _dataset_complete(args.dataset),
            "sha256": getattr(args, "dataset_sha256", None),
        },
        "run": {
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "limit": args.limit,
            "offset": getattr(args, "offset", 0),
            "resume": getattr(args, "resume", False),
            "max_runtime_hours": getattr(args, "max_runtime_hours", None),
            "fail_stop_count": getattr(args, "fail_stop_count", DEFAULT_FAIL_STOP_COUNT),
            "skip_ingest": args.skip_ingest,
            "maintenance_protocol": BENCHMARK_MAINTENANCE_PROTOCOL_VERSION,
            "qa_enabled": not args.no_qa,
            "reader_context_mode": getattr(args, "reader_context_mode", DEFAULT_READER_CONTEXT_MODE),
            "clean": args.clean,
            "config_compare": args.config_compare,
            "models": {
                "extractor": llm_identity["extractor_model"],
                "extractor_provider": llm_identity["extractor_provider"],
                "extractor_effective_provider": llm_identity["extractor_effective_provider"],
                "extractor_base_url": llm_identity["extractor_base_url"],
                "extractor_structured_mode": llm_identity["extractor_structured_mode"],
                "extractor_thinking": llm_identity["extractor_thinking"],
                "extractor_version": LLM_EXTRACTOR_VERSION,
                "extraction_fragment_protocol": EXTRACTION_FRAGMENT_PROTOCOL_VERSION,
                "extraction_chunk_target_chars": settings.extraction_chunk_target_chars,
                "extraction_chunk_overlap_turns": settings.extraction_chunk_overlap_turns,
                "reader_context_protocol": READER_CONTEXT_PROTOCOL_VERSION,
                "query_expansion_model": llm_identity["query_expansion_model"],
                "embedder": settings.embedding_model,
                "embedding_dim": settings.embedding_dim,
                "embedding_api_mode": settings.embedding_api_mode,
                "embedding_text_type": settings.embedding_text_type,
                "reranker": settings.reranker_model if settings.reranker_mode != "off" else "off",
                "reader": _qa_model(settings) if not args.no_qa else "not_run",
                "judge": _qa_model(settings) if not args.no_qa else "not_run",
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
    if abort_reason is not None:
        report["run"]["abort_reason"] = abort_reason
    return report


def _resume_case_with_claim_diagnostic_defaults(raw_case: Mapping[str, Any]) -> dict[str, Any]:
    case = dict(raw_case)
    raw_ingest = case.get("ingest")
    if not isinstance(raw_ingest, Mapping):
        return case
    ingest = dict(raw_ingest)
    defaults = _claim_diagnostic_defaults("unavailable_legacy_resume")
    diagnostic_fields = tuple(field for field in defaults if field != "claim_inflation_diagnostics_status")
    if all(field in ingest for field in diagnostic_fields):
        ingest.setdefault("claim_inflation_diagnostics_status", "computed_legacy_resume")
    else:
        # Legacy reports can contain the old write-result-based ratios.  If the
        # complete physical-row diagnostic is absent, do not preserve or infer
        # any member of that metric family.
        ingest = {**ingest, **defaults}
    case["ingest"] = ingest
    return case


def _load_resume_report(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not path.is_file():
        return None, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"resume output must contain a JSON object: {path}")
    if payload.get("schema_version") != 1 or payload.get("benchmark") != "LongMemEval-S":
        raise ValueError(f"resume output is not a LongMemEval-S schema v1 report: {path}")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(f"resume output cases must be a JSON array: {path}")
    cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError(f"resume output contains a non-object case: {path}")
        case_id = str(raw_case.get("case_id") or "").strip()
        if not case_id:
            raise ValueError(f"resume output contains a case without case_id: {path}")
        if case_id in case_ids:
            raise ValueError(f"resume output contains duplicate case_id {case_id!r}: {path}")
        case_ids.add(case_id)
        cases.append(_resume_case_with_claim_diagnostic_defaults(raw_case))
    return payload, cases


def _resume_model_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("resume output is missing run metadata")
    models = run.get("models")
    if not isinstance(models, Mapping):
        raise ValueError("resume output is missing run.models metadata")
    fields = (
        "extractor",
        "extractor_provider",
        "extractor_effective_provider",
        "extractor_base_url",
        "extractor_structured_mode",
        "extractor_thinking",
        "extractor_version",
        "extraction_fragment_protocol",
        "extraction_chunk_target_chars",
        "extraction_chunk_overlap_turns",
        "reader_context_protocol",
        "query_expansion_model",
        "embedder",
        "embedding_dim",
        "embedding_api_mode",
        "embedding_text_type",
        "reranker",
        "reader",
        "judge",
    )
    missing = [field for field in fields if field not in models]
    if missing:
        raise ValueError(f"resume output is missing model identity fields: {missing}")
    return {field: models[field] for field in fields}


def _validate_resume_report(
    report: Mapping[str, Any],
    args: argparse.Namespace,
    settings: Settings,
) -> None:
    dataset = report.get("dataset")
    previous_sha256 = dataset.get("sha256") if isinstance(dataset, Mapping) else None
    if not isinstance(previous_sha256, str) or not previous_sha256:
        raise ValueError("resume output is missing dataset.sha256")
    if previous_sha256 != args.dataset_sha256:
        raise ValueError("resume output dataset sha256 does not match --dataset")
    previous_identity = _resume_model_identity(report)
    llm_identity = _llm_configuration_identity(settings)
    reranker = settings.reranker_model if settings.reranker_mode != "off" else "off"
    qa_model = _qa_model(settings) if not args.no_qa else "not_run"
    expected_identity = {
        "extractor": llm_identity["extractor_model"],
        "extractor_provider": llm_identity["extractor_provider"],
        "extractor_effective_provider": llm_identity["extractor_effective_provider"],
        "extractor_base_url": llm_identity["extractor_base_url"],
        "extractor_structured_mode": llm_identity["extractor_structured_mode"],
        "extractor_thinking": llm_identity["extractor_thinking"],
        "extractor_version": LLM_EXTRACTOR_VERSION,
        "extraction_fragment_protocol": EXTRACTION_FRAGMENT_PROTOCOL_VERSION,
        "extraction_chunk_target_chars": settings.extraction_chunk_target_chars,
        "extraction_chunk_overlap_turns": settings.extraction_chunk_overlap_turns,
        "reader_context_protocol": READER_CONTEXT_PROTOCOL_VERSION,
        "query_expansion_model": llm_identity["query_expansion_model"],
        "embedder": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "embedding_api_mode": settings.embedding_api_mode,
        "embedding_text_type": settings.embedding_text_type,
        "reranker": reranker,
        "reader": qa_model,
        "judge": qa_model,
    }
    if previous_identity != expected_identity:
        raise ValueError("resume output model configuration does not match current settings")
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("resume output is missing run metadata")
    if run.get("package_version") != f"v{__version__}":
        raise ValueError("resume output package_version does not match current version")
    if run.get("maintenance_protocol") != BENCHMARK_MAINTENANCE_PROTOCOL_VERSION:
        raise ValueError("resume output maintenance_protocol does not match current version")
    if run.get("qa_enabled") is not (not args.no_qa):
        raise ValueError("resume output qa_enabled does not match current run")
    previous_context_mode = str(run.get("reader_context_mode") or "head")
    if previous_context_mode != args.reader_context_mode:
        raise ValueError("resume output reader_context_mode does not match current run")
    if run.get("offset") != args.offset:
        raise ValueError("resume output offset does not match --offset")
    if run.get("limit") != args.limit:
        raise ValueError("resume output limit does not match --limit")


def _validate_production_settings(settings: Settings) -> None:
    if "deepseek" in settings.llm_model.casefold() and settings.llm_structured_mode != "json_object":
        raise ValueError("DeepSeek benchmark extraction requires llm.structured_mode = 'json_object'")
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


def _validate_full_context_settings(settings: Settings) -> None:
    if not (os.environ.get("LLM_API_KEY") or settings.llm_api_key):
        raise ValueError("LLM_API_KEY is required for the full-context reader")


def _validate_full_context_args(args: argparse.Namespace) -> None:
    incompatible: list[str] = []
    if args.config_compare:
        incompatible.append("--config-compare")
    if args.skip_ingest:
        incompatible.append("--skip-ingest")
    if args.no_qa:
        incompatible.append("--no-qa")
    if args.clean:
        incompatible.append("--clean")
    if args.reader_context_mode != DEFAULT_READER_CONTEXT_MODE:
        incompatible.append("--reader-context-mode")
    if incompatible:
        raise ValueError(f"full-context mode does not support: {', '.join(incompatible)}")


def _validate_native_rag_settings(settings: Settings) -> None:
    if not (os.environ.get("LLM_API_KEY") or settings.llm_api_key):
        raise ValueError("LLM_API_KEY is required for the native-rag reader")
    if not (os.environ.get("EMBEDDING_API_KEY") or settings.embedding_api_key):
        raise ValueError("EMBEDDING_API_KEY is required for native-rag retrieval")
    if settings.embedder_mode != "real":
        raise ValueError("embedding.mode must be real for native-rag retrieval")
    if settings.embedding_model != "qwen3.7-text-embedding":
        raise ValueError("native-rag requires embedding.model = 'qwen3.7-text-embedding'")
    if settings.embedding_dim != 2048:
        raise ValueError("native-rag requires embedding.dim = 2048")
    if settings.embedding_api_mode != "native":
        raise ValueError("native-rag requires embedding.api_mode = 'native'")
    if settings.embedding_text_type not in {None, ""}:
        raise ValueError("native-rag requires embedding.text_type to be unset")


def _validate_native_rag_args(args: argparse.Namespace) -> None:
    incompatible: list[str] = []
    if args.config_compare:
        incompatible.append("--config-compare")
    if args.skip_ingest:
        incompatible.append("--skip-ingest")
    if args.no_qa:
        incompatible.append("--no-qa")
    if args.clean:
        incompatible.append("--clean")
    if args.reader_context_mode != DEFAULT_READER_CONTEXT_MODE:
        incompatible.append("--reader-context-mode")
    if incompatible:
        raise ValueError(f"native-rag mode does not support: {', '.join(incompatible)}")


def _full_context_usage_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "reader_tokens",
        "judge_tokens",
        "total_tokens",
        "reader_input_tokens",
        "reader_output_tokens",
        "reader_reasoning_tokens",
        "reader_answer_tokens",
        "judge_input_tokens",
        "judge_output_tokens",
        "judge_reasoning_tokens",
        "judge_answer_tokens",
        "total_input_tokens",
        "total_output_tokens",
        "total_reasoning_tokens",
        "total_answer_tokens",
    )
    usages = [
        result["qa"]["usage"]
        for result in results
        if isinstance(result.get("qa"), Mapping) and isinstance(result["qa"].get("usage"), Mapping)
    ]
    return {
        "reported_cases": len(usages),
        **{field: sum(int(usage.get(field, 0) or 0) for usage in usages) for field in fields},
    }


def _full_context_cost_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    costs = [result["cost"] for result in results if isinstance(result.get("cost"), Mapping)]
    priced = [cost for cost in costs if cost.get("priced") is True and cost.get("total_cny") is not None]
    return {
        "currency": "CNY",
        "reported_cases": len(costs),
        "priced_cases": len(priced),
        "unpriced_cases": len(costs) - len(priced),
        "total_cny": round(sum(float(cost["total_cny"]) for cost in priced), 6) if priced else None,
    }


def _full_context_latency_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    latencies = [
        result["qa"]["latency_seconds"]
        for result in results
        if isinstance(result.get("qa"), Mapping) and isinstance(result["qa"].get("latency_seconds"), Mapping)
    ]
    totals = [float(item.get("total", 0.0) or 0.0) for item in latencies]
    return {
        "reported_cases": len(latencies),
        "total_seconds": round(sum(totals), 3),
        "mean_seconds": round(mean(totals), 3) if totals else None,
        "median_seconds": round(median(totals), 3) if totals else None,
    }


def _full_context_report(
    args: argparse.Namespace,
    settings: Settings,
    results: Sequence[Mapping[str, Any]],
    started_at: str,
    status: str,
    abort_reason: str | None = None,
) -> dict[str, Any]:
    model = _qa_model(settings)
    report: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "control": "full-context",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(args.dataset.resolve()),
            "bytes": args.dataset.stat().st_size if args.dataset.is_file() else None,
            "complete_json_array": _dataset_complete(args.dataset),
            "sha256": args.dataset_sha256,
        },
        "run": {
            "mode": "full-context",
            "control_protocol": FULL_CONTEXT_PROTOCOL_VERSION,
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "limit": args.limit,
            "offset": args.offset,
            "resume": args.resume,
            "max_runtime_hours": args.max_runtime_hours,
            "fail_stop_count": args.fail_stop_count,
            "selector": "all-sessions",
            "session_order": "occurred_at ascending; source index breaks timestamp ties",
            "session_format": "timestamped compact JSON role/content messages",
            "truncation": "none",
            "reader_timeout_seconds": int(FULL_CONTEXT_READER_TIMEOUT_SECONDS),
            "models": {
                "reader": model,
                "judge": model,
                "reader_thinking": True,
                "reader_thinking_budget": READER_THINKING_TOKEN_BUDGET,
                "reader_answer_budget": READER_ANSWER_TOKEN_BUDGET,
                "judge_thinking": False,
                "judge_max_tokens": READER_ANSWER_TOKEN_BUDGET,
                "judge_prompt_version": LONGMEMEVAL_JUDGE_PROMPT_VERSION,
            },
        },
        "metrics": aggregate_results(results),
        "usage": _full_context_usage_summary(results),
        "latency": _full_context_latency_summary(results),
        "cost": _full_context_cost_summary(results),
        "cases": list(results),
    }
    if abort_reason is not None:
        report["run"]["abort_reason"] = abort_reason
    return report


def _validate_full_context_resume_report(
    report: Mapping[str, Any],
    args: argparse.Namespace,
    settings: Settings,
) -> None:
    if report.get("control") != "full-context":
        raise ValueError("resume output is not a full-context control report")
    dataset = report.get("dataset")
    previous_sha256 = dataset.get("sha256") if isinstance(dataset, Mapping) else None
    if previous_sha256 != args.dataset_sha256:
        raise ValueError("resume output dataset sha256 does not match --dataset")
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("resume output is missing run metadata")
    models = run.get("models")
    if not isinstance(models, Mapping):
        raise ValueError("resume output is missing run.models metadata")
    expected = {
        "mode": "full-context",
        "control_protocol": FULL_CONTEXT_PROTOCOL_VERSION,
        "package_version": f"v{__version__}",
        "limit": args.limit,
        "offset": args.offset,
        "reader_timeout_seconds": int(FULL_CONTEXT_READER_TIMEOUT_SECONDS),
    }
    for field, value in expected.items():
        if run.get(field) != value:
            raise ValueError(f"resume output {field} does not match current full-context run")
    expected_models = {
        "reader": _qa_model(settings),
        "judge": _qa_model(settings),
        "reader_thinking": True,
        "reader_thinking_budget": READER_THINKING_TOKEN_BUDGET,
        "reader_answer_budget": READER_ANSWER_TOKEN_BUDGET,
        "judge_thinking": False,
        "judge_max_tokens": READER_ANSWER_TOKEN_BUDGET,
        "judge_prompt_version": LONGMEMEVAL_JUDGE_PROMPT_VERSION,
    }
    if dict(models) != expected_models:
        raise ValueError("resume output model configuration does not match current full-context settings")


def _run_full_context_control(
    args: argparse.Namespace,
    settings: Settings,
    invocation_started: float,
    generated_started_at: str,
) -> int:
    resume_report: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    if args.resume:
        resume_report, results = _load_resume_report(args.output)
        if resume_report is not None:
            _validate_full_context_resume_report(resume_report, args, settings)
            results = [result for result in results if _result_error_type(result) not in {"http_429", "quota"}]
    started_at = generated_started_at
    if resume_report is not None:
        previous_run = resume_report.get("run")
        if isinstance(previous_run, Mapping) and isinstance(previous_run.get("started_at"), str):
            started_at = previous_run["started_at"]

    completed = {str(result["case_id"]): result for result in results}
    total_hint = str(args.limit) if args.limit is not None else "all"
    runtime_seconds = args.max_runtime_hours * 3600.0 if args.max_runtime_hours is not None else None
    print(
        f"LongMemEval-S control=full-context reader={_qa_model(settings)} "
        f"offset={args.offset} limit={total_hint} resume={args.resume} "
        f"timeout={int(FULL_CONTEXT_READER_TIMEOUT_SECONDS)}s",
        flush=True,
    )
    selected_case_ids: list[str] = []
    consecutive_failure_type: str | None = None
    consecutive_failure_count = 0
    abort_reason: str | None = None
    try:
        for case_number, record in enumerate(iter_case_records(args.dataset, args.limit, args.offset), start=1):
            case = normalize_case(record)
            selected_case_ids.append(case.case_id)
            case_result = completed.get(case.case_id)
            case_was_run = False
            if case_result is not None:
                print(f"[{case_number}/{total_hint}] {case.case_id}: resume skip", flush=True)
            else:
                if runtime_seconds is not None and time.monotonic() - invocation_started >= runtime_seconds:
                    abort_reason = f"max runtime of {args.max_runtime_hours:g} hours reached"
                    break
                case_result = _run_full_context_case(case, settings)
                results.append(case_result)
                completed[case.case_id] = case_result
                _write_json_atomic(
                    args.output,
                    _full_context_report(args, settings, results, started_at, "running"),
                )
                case_was_run = True
                failure_type = _result_error_type(case_result)
                if failure_type is None:
                    consecutive_failure_type = None
                    consecutive_failure_count = 0
                elif failure_type == consecutive_failure_type:
                    consecutive_failure_count += 1
                else:
                    consecutive_failure_type = failure_type
                    consecutive_failure_count = 1
            retrieval = case_result.get("retrieval") or {}
            qa = case_result.get("qa") or {}
            usage = qa.get("usage") or {}
            print(
                f"[{case_number}/{total_hint}] {case.case_id}: "
                f"sessions={retrieval.get('sessions_selected')} "
                f"reader_input={usage.get('reader_input_tokens')} correct={qa.get('correct')} "
                f"error={case_result.get('error')}",
                flush=True,
            )
            if case_was_run and consecutive_failure_count >= args.fail_stop_count:
                abort_reason = (
                    f"circuit breaker opened after {consecutive_failure_count} consecutive "
                    f"{consecutive_failure_type} failures"
                )
                break
            if runtime_seconds is not None and time.monotonic() - invocation_started >= runtime_seconds:
                abort_reason = f"max runtime of {args.max_runtime_hours:g} hours reached"
                break
        if abort_reason is None:
            unexpected = set(completed) - set(selected_case_ids)
            if unexpected:
                raise ValueError(f"resume output contains case_ids outside the selected shard: {sorted(unexpected)}")
    except Exception:
        _write_json_atomic(
            args.output,
            _full_context_report(args, settings, results, started_at, "aborted", "unhandled exception"),
        )
        raise

    if not selected_case_ids:
        raise ValueError("LongMemEval dataset contains no selected cases")
    if abort_reason is not None:
        report = _full_context_report(args, settings, results, started_at, "aborted", abort_reason)
        _write_json_atomic(args.output, report)
        print(f"aborted reason={abort_reason} cases={len(results)} output={args.output}", flush=True)
        return 2

    selected_order = {case_id: index for index, case_id in enumerate(selected_case_ids)}
    results.sort(key=lambda result: selected_order[str(result["case_id"])])
    report = _full_context_report(args, settings, results, started_at, "completed")
    _write_json_atomic(args.output, report)
    overall = report["metrics"]["overall"]
    print(
        f"completed control=full-context cases={overall['cases']} failures={overall['failed_cases']} "
        f"QA={overall['qa_accuracy']} input_tokens={report['usage']['reader_input_tokens']} "
        f"cost_cny={report['cost']['total_cny']} output={args.output}",
        flush=True,
    )
    return 1 if overall["failed_cases"] else 0


def _native_rag_embedding_usage_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    usages = [
        result["embedding"]["usage"]
        for result in results
        if isinstance(result.get("embedding"), Mapping) and isinstance(result["embedding"].get("usage"), Mapping)
    ]
    fields = (
        "api_calls",
        "tokens",
        "network_api_calls_this_run",
        "cache_hit_batches",
        "db_cached_vectors",
    )
    document_usages = [usage["document"] for usage in usages if isinstance(usage.get("document"), Mapping)]
    query_usages = [usage["query"] for usage in usages if isinstance(usage.get("query"), Mapping)]
    return {
        "reported_cases": len(usages),
        **{field: sum(int(usage.get(field, 0) or 0) for usage in usages) for field in fields},
        **{f"document_{field}": sum(int(usage.get(field, 0) or 0) for usage in document_usages) for field in fields},
        **{f"query_{field}": sum(int(usage.get(field, 0) or 0) for usage in query_usages) for field in fields},
    }


def _native_rag_cost_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    costs = [result["cost"] for result in results if isinstance(result.get("cost"), Mapping)]
    priced = [cost for cost in costs if cost.get("priced") is True and cost.get("total_cny") is not None]

    def total(field: str) -> float | None:
        values = [float(cost[field]) for cost in costs if cost.get(field) is not None]
        return round(sum(values), 6) if values else None

    return {
        "currency": "CNY",
        "reported_cases": len(costs),
        "priced_cases": len(priced),
        "unpriced_cases": len(costs) - len(priced),
        "index_embedding_cny": total("index_embedding_cny"),
        "query_embedding_cny": total("query_embedding_cny"),
        "embedding_cny": total("embedding_cny"),
        "online_query_cny": total("online_query_cny"),
        "cold_start_total_cny": total("cold_start_total_cny"),
        "total_cny": round(sum(float(cost["total_cny"]) for cost in priced), 6) if priced else None,
    }


def _native_rag_latency_summary(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    embedding_totals = [
        float(result["embedding"]["latency_seconds"].get("total_wall", 0.0) or 0.0)
        for result in results
        if isinstance(result.get("embedding"), Mapping)
        and isinstance(result["embedding"].get("latency_seconds"), Mapping)
    ]
    qa_totals = [
        float(result["qa"]["latency_seconds"].get("total", 0.0) or 0.0)
        for result in results
        if isinstance(result.get("qa"), Mapping) and isinstance(result["qa"].get("latency_seconds"), Mapping)
    ]
    return {
        "embedding_reported_cases": len(embedding_totals),
        "embedding_total_seconds": round(sum(embedding_totals), 3),
        "embedding_mean_seconds": round(mean(embedding_totals), 3) if embedding_totals else None,
        "qa_reported_cases": len(qa_totals),
        "qa_total_seconds": round(sum(qa_totals), 3),
        "qa_mean_seconds": round(mean(qa_totals), 3) if qa_totals else None,
    }


def _native_rag_report(
    args: argparse.Namespace,
    settings: Settings,
    results: Sequence[Mapping[str, Any]],
    started_at: str,
    status: str,
    abort_reason: str | None = None,
) -> dict[str, Any]:
    model = _qa_model(settings)
    qa_usage = _full_context_usage_summary(results)
    embedding_usage = _native_rag_embedding_usage_summary(results)
    report: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "LongMemEval-S",
        "control": "native-rag",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(args.dataset.resolve()),
            "bytes": args.dataset.stat().st_size if args.dataset.is_file() else None,
            "complete_json_array": _dataset_complete(args.dataset),
            "sha256": args.dataset_sha256,
        },
        "run": {
            "mode": "native-rag",
            "control_protocol": NATIVE_RAG_PROTOCOL_VERSION,
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "limit": args.limit,
            "offset": args.offset,
            "resume": args.resume,
            "max_runtime_hours": args.max_runtime_hours,
            "fail_stop_count": args.fail_stop_count,
            "selector": "exact-cosine",
            "retrieval_unit": "complete raw session",
            "top_k": NATIVE_RAG_TOP_K,
            "query": "question text only",
            "reader_order": "occurred_at ascending; source index breaks timestamp ties",
            "session_format": "timestamped compact JSON role/content messages",
            "truncation": "none",
            "embedding_cache": "content-addressed NPZ batches; usage preserves logical cold-start tokens",
            "embedding_timeout_seconds": int(NATIVE_RAG_EMBEDDING_TIMEOUT_SECONDS),
            "reader_timeout_seconds": int(FULL_CONTEXT_READER_TIMEOUT_SECONDS),
            "models": {
                "embedder": settings.embedding_model,
                "embedding_dim": settings.embedding_dim,
                "embedding_api_mode": settings.embedding_api_mode,
                "embedding_text_type": None,
                "embedding_query_instruct": None,
                "reader": model,
                "judge": model,
                "reader_thinking": True,
                "reader_thinking_budget": READER_THINKING_TOKEN_BUDGET,
                "reader_answer_budget": READER_ANSWER_TOKEN_BUDGET,
                "judge_thinking": False,
                "judge_max_tokens": READER_ANSWER_TOKEN_BUDGET,
                "judge_prompt_version": LONGMEMEVAL_JUDGE_PROMPT_VERSION,
            },
        },
        "metrics": aggregate_results(results),
        "usage": {
            "embedding": embedding_usage,
            "qa": qa_usage,
            "logical_total_tokens": embedding_usage["tokens"] + qa_usage["total_tokens"],
        },
        "latency": _native_rag_latency_summary(results),
        "cost": _native_rag_cost_summary(results),
        "cases": list(results),
    }
    if abort_reason is not None:
        report["run"]["abort_reason"] = abort_reason
    return report


def _validate_native_rag_resume_report(
    report: Mapping[str, Any],
    args: argparse.Namespace,
    settings: Settings,
) -> None:
    if report.get("control") != "native-rag":
        raise ValueError("resume output is not a native-rag control report")
    dataset = report.get("dataset")
    previous_sha256 = dataset.get("sha256") if isinstance(dataset, Mapping) else None
    if previous_sha256 != args.dataset_sha256:
        raise ValueError("resume output dataset sha256 does not match --dataset")
    run = report.get("run")
    if not isinstance(run, Mapping):
        raise ValueError("resume output is missing run metadata")
    models = run.get("models")
    if not isinstance(models, Mapping):
        raise ValueError("resume output is missing run.models metadata")
    expected = {
        "mode": "native-rag",
        "control_protocol": NATIVE_RAG_PROTOCOL_VERSION,
        "package_version": f"v{__version__}",
        "limit": args.limit,
        "offset": args.offset,
        "top_k": NATIVE_RAG_TOP_K,
        "embedding_timeout_seconds": int(NATIVE_RAG_EMBEDDING_TIMEOUT_SECONDS),
        "reader_timeout_seconds": int(FULL_CONTEXT_READER_TIMEOUT_SECONDS),
    }
    for field, value in expected.items():
        if run.get(field) != value:
            raise ValueError(f"resume output {field} does not match current native-rag run")
    expected_models = {
        "embedder": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "embedding_api_mode": settings.embedding_api_mode,
        "embedding_text_type": None,
        "embedding_query_instruct": None,
        "reader": _qa_model(settings),
        "judge": _qa_model(settings),
        "reader_thinking": True,
        "reader_thinking_budget": READER_THINKING_TOKEN_BUDGET,
        "reader_answer_budget": READER_ANSWER_TOKEN_BUDGET,
        "judge_thinking": False,
        "judge_max_tokens": READER_ANSWER_TOKEN_BUDGET,
        "judge_prompt_version": LONGMEMEVAL_JUDGE_PROMPT_VERSION,
    }
    if dict(models) != expected_models:
        raise ValueError("resume output model configuration does not match current native-rag settings")


def _run_native_rag_control(
    args: argparse.Namespace,
    settings: Settings,
    invocation_started: float,
    generated_started_at: str,
) -> int:
    resume_report: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    if args.resume:
        resume_report, results = _load_resume_report(args.output)
        if resume_report is not None:
            _validate_native_rag_resume_report(resume_report, args, settings)
            results = [result for result in results if _result_error_type(result) not in {"http_429", "quota"}]
    started_at = generated_started_at
    if resume_report is not None:
        previous_run = resume_report.get("run")
        if isinstance(previous_run, Mapping) and isinstance(previous_run.get("started_at"), str):
            started_at = previous_run["started_at"]

    embedding_api_key = os.environ.get("EMBEDDING_API_KEY") or settings.embedding_api_key
    if not embedding_api_key:
        raise ValueError("EMBEDDING_API_KEY is required for native-rag retrieval")
    embedding_client = DashScopeEmbeddingClient(
        str(embedding_api_key),
        base_url=settings.embedding_base_url,
        timeout_seconds=NATIVE_RAG_EMBEDDING_TIMEOUT_SECONDS,
        max_attempts=settings.embedding_max_attempts,
    )
    embedding_config = _native_rag_embedding_config(settings)
    completed = {str(result["case_id"]): result for result in results}
    total_hint = str(args.limit) if args.limit is not None else "all"
    runtime_seconds = args.max_runtime_hours * 3600.0 if args.max_runtime_hours is not None else None
    print(
        f"LongMemEval-S control=native-rag embedder={embedding_config.model} "
        f"reader={_qa_model(settings)} top_k={NATIVE_RAG_TOP_K} "
        f"offset={args.offset} limit={total_hint} resume={args.resume}",
        flush=True,
    )
    selected_case_ids: list[str] = []
    consecutive_failure_type: str | None = None
    consecutive_failure_count = 0
    abort_reason: str | None = None
    try:
        for case_number, record in enumerate(iter_case_records(args.dataset, args.limit, args.offset), start=1):
            case = normalize_case(record)
            selected_case_ids.append(case.case_id)
            case_result = completed.get(case.case_id)
            case_was_run = False
            if case_result is not None:
                print(f"[{case_number}/{total_hint}] {case.case_id}: resume skip", flush=True)
            else:
                if runtime_seconds is not None and time.monotonic() - invocation_started >= runtime_seconds:
                    abort_reason = f"max runtime of {args.max_runtime_hours:g} hours reached"
                    break
                case_result = _run_native_rag_case(case, settings, embedding_client, embedding_config)
                results.append(case_result)
                completed[case.case_id] = case_result
                _write_json_atomic(
                    args.output,
                    _native_rag_report(args, settings, results, started_at, "running"),
                )
                case_was_run = True
                failure_type = _result_error_type(case_result)
                if failure_type is None:
                    consecutive_failure_type = None
                    consecutive_failure_count = 0
                elif failure_type == consecutive_failure_type:
                    consecutive_failure_count += 1
                else:
                    consecutive_failure_type = failure_type
                    consecutive_failure_count = 1
            retrieval = case_result.get("retrieval") or {}
            qa = case_result.get("qa") or {}
            usage = qa.get("usage") or {}
            print(
                f"[{case_number}/{total_hint}] {case.case_id}: "
                f"sessions={retrieval.get('sessions_selected')} "
                f"session_R@10={retrieval.get('session_recall_at_10')} "
                f"reader_input={usage.get('reader_input_tokens')} correct={qa.get('correct')} "
                f"error={case_result.get('error')}",
                flush=True,
            )
            if case_was_run and consecutive_failure_count >= args.fail_stop_count:
                abort_reason = (
                    f"circuit breaker opened after {consecutive_failure_count} consecutive "
                    f"{consecutive_failure_type} failures"
                )
                break
            if runtime_seconds is not None and time.monotonic() - invocation_started >= runtime_seconds:
                abort_reason = f"max runtime of {args.max_runtime_hours:g} hours reached"
                break
        if abort_reason is None:
            unexpected = set(completed) - set(selected_case_ids)
            if unexpected:
                raise ValueError(f"resume output contains case_ids outside the selected shard: {sorted(unexpected)}")
    except Exception:
        _write_json_atomic(
            args.output,
            _native_rag_report(args, settings, results, started_at, "aborted", "unhandled exception"),
        )
        raise
    finally:
        embedding_client.close()

    if not selected_case_ids:
        raise ValueError("LongMemEval dataset contains no selected cases")
    if abort_reason is not None:
        report = _native_rag_report(args, settings, results, started_at, "aborted", abort_reason)
        _write_json_atomic(args.output, report)
        print(f"aborted reason={abort_reason} cases={len(results)} output={args.output}", flush=True)
        return 2

    selected_order = {case_id: index for index, case_id in enumerate(selected_case_ids)}
    results.sort(key=lambda result: selected_order[str(result["case_id"])])
    report = _native_rag_report(args, settings, results, started_at, "completed")
    _write_json_atomic(args.output, report)
    overall = report["metrics"]["overall"]
    print(
        f"completed control=native-rag cases={overall['cases']} failures={overall['failed_cases']} "
        f"QA={overall['qa_accuracy']} session_R@10={overall['session_recall_at_10']} "
        f"logical_tokens={report['usage']['logical_total_tokens']} "
        f"cost_cny={report['cost']['total_cny']} output={args.output}",
        flush=True,
    )
    return 1 if overall["failed_cases"] else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be a positive integer")
    if args.offset < 0:
        raise ValueError("--offset must be a non-negative integer")
    if args.max_runtime_hours is not None and args.max_runtime_hours <= 0:
        raise ValueError("--max-runtime-hours must be positive")
    if args.fail_stop_count < 1:
        raise ValueError("--fail-stop-count must be a positive integer")
    if args.mode == "full-context":
        _validate_full_context_args(args)
    elif args.mode == "native-rag":
        _validate_native_rag_args(args)
    if args.config_compare and (
        args.offset
        or args.resume
        or args.max_runtime_hours is not None
        or args.fail_stop_count != DEFAULT_FAIL_STOP_COUNT
    ):
        raise ValueError(
            "--offset, --resume, --max-runtime-hours, and non-default --fail-stop-count "
            "are not supported with --config-compare"
        )
    if args.limit is None and not _dataset_complete(args.dataset):
        raise ValueError(
            "LongMemEval dataset is missing or incomplete; wait for the top-level JSON array to finish downloading"
        )
    invocation_started = time.monotonic()
    args.dataset_sha256 = _file_sha256(args.dataset)
    settings = load_settings(args.config, args.env_file)
    generated_started_at = datetime.now(timezone.utc).isoformat()
    if args.mode == "full-context":
        _validate_full_context_settings(settings)
        return _run_full_context_control(args, settings, invocation_started, generated_started_at)
    if args.mode == "native-rag":
        _validate_native_rag_settings(settings)
        return _run_native_rag_control(args, settings, invocation_started, generated_started_at)
    settings = dataclasses.replace(settings, vector_backend="sqlite_scan")
    _validate_production_settings(settings)
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    if args.config_compare:
        return _run_config_compare(args, settings, embedder, reranker, generated_started_at)

    resume_report: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    if args.resume:
        resume_report, results = _load_resume_report(args.output)
        if resume_report is not None:
            _validate_resume_report(resume_report, args, settings)
            results = [result for result in results if _result_error_type(result) not in {"http_429", "quota"}]
    started_at = generated_started_at
    if resume_report is not None:
        previous_run = resume_report.get("run")
        if isinstance(previous_run, Mapping) and isinstance(previous_run.get("started_at"), str):
            started_at = previous_run["started_at"]

    completed = {str(result["case_id"]): result for result in results}
    total_hint = str(args.limit) if args.limit is not None else "all"
    runtime_seconds = args.max_runtime_hours * 3600.0 if args.max_runtime_hours is not None else None

    print(
        f"LongMemEval-S model={settings.llm_model} embedder={settings.embedding_model} "
        f"prompt={LLM_EXTRACTOR_VERSION} offset={args.offset} limit={total_hint} "
        f"resume={args.resume} qa={not args.no_qa} reader_context={args.reader_context_mode}",
        flush=True,
    )
    selected_case_ids: list[str] = []
    consecutive_failure_type: str | None = None
    consecutive_failure_count = 0
    abort_reason: str | None = None
    try:
        for case_number, record in enumerate(
            iter_case_records(args.dataset, args.limit, args.offset),
            start=1,
        ):
            case = normalize_case(record)
            selected_case_ids.append(case.case_id)
            case_result = completed.get(case.case_id)
            case_was_run = False
            if case_result is not None:
                print(f"[{case_number}/{total_hint}] {case.case_id}: resume skip", flush=True)
            else:
                if runtime_seconds is not None and time.monotonic() - invocation_started >= runtime_seconds:
                    abort_reason = f"max runtime of {args.max_runtime_hours:g} hours reached"
                    break
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
                    reader_context_mode=args.reader_context_mode,
                )
                results.append(case_result)
                completed[case.case_id] = case_result
                _write_json_atomic(args.output, _report(args, settings, results, started_at, "running"))
                case_was_run = True
                failure_type = _result_error_type(case_result)
                if failure_type is None:
                    consecutive_failure_type = None
                    consecutive_failure_count = 0
                elif failure_type == consecutive_failure_type:
                    consecutive_failure_count += 1
                else:
                    consecutive_failure_type = failure_type
                    consecutive_failure_count = 1
            retrieval = case_result.get("retrieval") or {}
            print(
                f"[{case_number}/{total_hint}] {case.case_id}: "
                f"R@10={retrieval.get('recall_at_10')} MRR={retrieval.get('mrr')} "
                f"error={case_result.get('error')}",
                flush=True,
            )
            if case_was_run and consecutive_failure_count >= args.fail_stop_count:
                abort_reason = (
                    f"circuit breaker opened after {consecutive_failure_count} consecutive "
                    f"{consecutive_failure_type} failures"
                )
                break
            if runtime_seconds is not None and time.monotonic() - invocation_started >= runtime_seconds:
                abort_reason = f"max runtime of {args.max_runtime_hours:g} hours reached"
                break
        if abort_reason is None:
            unexpected = set(completed) - set(selected_case_ids)
            if unexpected:
                raise ValueError(f"resume output contains case_ids outside the selected shard: {sorted(unexpected)}")
    except Exception:
        _write_json_atomic(
            args.output,
            _report(args, settings, results, started_at, "aborted", "unhandled exception"),
        )
        raise

    if not selected_case_ids:
        raise ValueError("LongMemEval dataset contains no selected cases")
    if abort_reason is not None:
        report = _report(args, settings, results, started_at, "aborted", abort_reason)
        _write_json_atomic(args.output, report)
        print(f"aborted reason={abort_reason} cases={len(results)} output={args.output}", flush=True)
        return 2

    selected_order = {case_id: index for index, case_id in enumerate(selected_case_ids)}
    results.sort(key=lambda result: selected_order[str(result["case_id"])])
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
