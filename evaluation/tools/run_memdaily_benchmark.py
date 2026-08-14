#!/usr/bin/env python
"""Run MemDaily benchmark against hl_mem's production extraction and recall stack.

Pipeline per trajectory:
  1. Ingest: each message in message_list → hl_mem event + LLM extraction
  2. Recall: QA.question → hl_mem recall → retrieved claims
  3. QA: retrieved claims → LLM answer
  4. Score: QA accuracy (char-level F1 + choice match), Recall@5 (gold evidence)
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hl_mem import __version__  # noqa: E402
from hl_mem.application.answerability import Answerability, abstention_kind  # noqa: E402
from hl_mem.application.ingest import IngestService  # noqa: E402
from hl_mem.application.recall import RecallService  # noqa: E402
from hl_mem.components import (  # noqa: E402
    initialize_process,
    make_embedder,
    make_extractor,
    make_query_expander,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.evaluation.memdaily import (  # noqa: E402
    QUESTION_TYPES,
    MemDailyAdapter,
    parse_memdaily_timestamp,
)
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION  # noqa: E402
from hl_mem.llm.types import StructuredOutputMode  # noqa: E402
from hl_mem.recall.relation_expansion import RelationExpansionConfig  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402

DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "memdaily_smoke.json"
DEFAULT_CONFIG = ROOT / "hl_mem.toml"
DEFAULT_ENV_FILE = ROOT / ".env"
DATABASE_ROOT = ROOT / "var" / "benchmark_memdaily"
RECALL_K = 10  # recall limit; recall@5 computed from top-5
QA_FALLBACK_MODEL = "qwen3.7-plus"


# ─── Normalization & data structures ──────────────────────────────────────────


@dataclass(frozen=True)
class MemDailyMessage:
    """One MemDaily message represented as one hl_mem event."""

    mid: int
    event_id: str
    occurred_at: str
    text: str
    place: str


@dataclass(frozen=True)
class MemDailyTrajectory:
    """Normalized MemDaily trajectory ready for benchmark execution."""

    case_id: str
    qtype: str
    subtype: str
    tid: int
    namespace: str
    question: str
    answer: str
    question_at: str | None
    ground_truth_choice: str | None
    choices: dict[str, str]
    messages: tuple[MemDailyMessage, ...]
    gold_event_ids: tuple[str, ...]


# ─── Data loading ─────────────────────────────────────────────────────────────


def load_trajectories(
    source: Path,
    subset: str = "events",
    n_per_type: int | None = None,
    qtypes: Sequence[str] | None = None,
) -> list[MemDailyTrajectory]:
    """Load MemDaily trajectories from source JSON.

    Args:
        source: path to memdaily.json
        subset: subtype to select (default: events)
        n_per_type: max trajectories per question type (None = all)
        qtypes: optional filter — only load these question types (None = all)
    """
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("MemDaily source must be a JSON object keyed by question type")

    selected_types = qtypes if qtypes else QUESTION_TYPES
    trajectories: list[MemDailyTrajectory] = []
    counts_by_type: dict[str, int] = defaultdict(int)

    for qtype in selected_types:
        type_data = raw.get(qtype)
        if not isinstance(type_data, Mapping):
            continue
        subtype_data = type_data.get(subset)
        if not isinstance(subtype_data, Sequence) or isinstance(subtype_data, (str, bytes)):
            continue
        for trajectory in subtype_data:
            if not isinstance(trajectory, Mapping):
                continue
            if n_per_type is not None and counts_by_type[qtype] >= n_per_type:
                break
            norm = _normalize_trajectory(qtype, subset, trajectory)
            trajectories.append(norm)
            counts_by_type[qtype] += 1

    return trajectories


def _normalize_trajectory(qtype: str, subtype: str, trajectory: Mapping[str, Any]) -> MemDailyTrajectory:
    """Convert raw MemDaily trajectory dict to MemDailyTrajectory."""
    tid = int(trajectory.get("tid", 0))
    case_id = f"memdaily:{qtype}:{subtype}:{tid}"
    namespace = MemDailyAdapter.case_namespace(qtype, subtype, tid)

    message_list = trajectory.get("message_list") or []
    if not isinstance(message_list, Sequence) or isinstance(message_list, (str, bytes)):
        raise ValueError(f"trajectory {case_id}: message_list must be a list")

    messages: list[MemDailyMessage] = []
    mid_to_event_id: dict[int, str] = {}
    for index, msg in enumerate(message_list):
        if not isinstance(msg, Mapping):
            continue
        mid = int(msg.get("mid", index))
        text = str(msg.get("message") or "")
        time_str = str(msg.get("time") or "")
        place = str(msg.get("place") or "")
        occurred_at = parse_memdaily_timestamp(time_str)
        event_id = f"memdaily:{qtype}:{subtype}:{tid}:mid:{mid}"
        messages.append(MemDailyMessage(mid=mid, event_id=event_id, occurred_at=occurred_at, text=text, place=place))
        mid_to_event_id[mid] = event_id

    qa = trajectory.get("QA") or {}
    if not isinstance(qa, Mapping):
        qa = {}

    question = str(qa.get("question") or "")
    answer = str(qa.get("answer") or "").strip()
    qa_time = str(qa.get("time") or "").strip()
    question_at = parse_memdaily_timestamp(qa_time) if qa_time else None
    ground_truth = str(qa.get("ground_truth") or "").strip() or None
    choices_raw = qa.get("choices") or {}
    choices = {str(k): str(v) for k, v in choices_raw.items()} if isinstance(choices_raw, Mapping) else {}

    target_step_ids = qa.get("target_step_id") or []
    if not isinstance(target_step_ids, Sequence) or isinstance(target_step_ids, (str, bytes)):
        target_step_ids = []
    gold_event_ids = tuple(
        dict.fromkeys(mid_to_event_id[int(mid)] for mid in target_step_ids if int(mid) in mid_to_event_id)
    )

    return MemDailyTrajectory(
        case_id=case_id,
        qtype=qtype,
        subtype=subtype,
        tid=tid,
        namespace=namespace,
        question=question,
        answer=answer,
        question_at=question_at,
        ground_truth_choice=ground_truth,
        choices=choices,
        messages=tuple(messages),
        gold_event_ids=gold_event_ids,
    )


# ─── QA scoring ───────────────────────────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: strip punctuation, whitespace, lowercase."""
    # Remove common Chinese and English punctuation
    cleaned = re.sub(r"""[，。！？；：、"'（）【】《》\s,.!?;:()\[\]<>]""", "", text)
    return cleaned.lower().strip()


def score_qa_accuracy(
    llm_answer: str,
    gold_answer: str,
    choices: Mapping[str, str] | None = None,
    ground_truth_choice: str | None = None,
) -> dict[str, float | bool | str]:
    """Score QA accuracy: char-level F1 + exact match + choice accuracy.

    For Chinese text, char-level tokenization is used (no jieba dependency).
    """
    norm_pred = _normalize_text(llm_answer)
    norm_gold = _normalize_text(gold_answer)

    # 1. Exact match (normalized)
    exact_match = norm_pred == norm_gold and bool(norm_gold)

    # 2. Char-level token-level F1
    pred_chars = list(norm_pred)
    gold_chars = list(norm_gold)
    if not pred_chars or not gold_chars:
        f1 = 0.0
    else:
        pred_counter = Counter(pred_chars)
        gold_counter = Counter(gold_chars)
        overlap = sum((pred_counter & gold_counter).values())
        if overlap == 0:
            f1 = 0.0
        else:
            precision = overlap / len(pred_chars)
            recall = overlap / len(gold_chars)
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    # 3. Choice accuracy: does the LLM answer match the gold choice text?
    choice_correct: bool | None = None
    if choices and ground_truth_choice:
        gold_choice_text = _normalize_text(choices.get(ground_truth_choice, ""))
        # Check if the LLM answer contains the gold choice text
        # or starts with the choice letter
        if gold_choice_text and gold_choice_text in norm_pred:
            choice_correct = True
        elif ground_truth_choice.upper() in llm_answer.strip().upper()[:3]:
            # The LLM explicitly picked the letter
            choice_correct = True
        else:
            choice_correct = False

    return {
        "exact_match": exact_match,
        "f1": round(f1, 4),
        "choice_correct": choice_correct,
    }


def score_recall_at_k(
    retrieved_evidence_ids: Sequence[Sequence[str]],
    gold_event_ids: Sequence[str],
    k: int = 5,
) -> float:
    """Compute Recall@k: fraction of gold event IDs found in top-k results.

    Each item in retrieved_evidence_ids is the list of evidence_event_ids
    for one retrieved claim, in rank order.
    """
    gold = set(gold_event_ids)
    if not gold or k <= 0:
        return 0.0
    found: set[str] = set()
    for evidence_ids in retrieved_evidence_ids[:k]:
        found.update(evidence_ids)
    return len(found & gold) / len(gold)


# ─── Ingest ───────────────────────────────────────────────────────────────────


def _safe_case_name(case_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", case_id).strip("._-")[:72] or "case"
    return name


def _case_db_path(case_id: str) -> Path:
    return DATABASE_ROOT / f"{_safe_case_name(case_id)}.db"


def _case_manifest_path(db_path: Path) -> Path:
    return db_path.with_suffix(".manifest.json")


def _case_fingerprint(traj: MemDailyTrajectory) -> str:
    payload = {
        "case_id": traj.case_id,
        "namespace": traj.namespace,
        "messages": [
            {
                "mid": message.mid,
                "event_id": message.event_id,
                "occurred_at": message.occurred_at,
                "text": message.text,
                "place": message.place,
            }
            for message in traj.messages
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entity_alias_file_identity(path_value: str | None) -> dict[str, str | None] | None:
    if path_value is None:
        return None
    path = Path(path_value).expanduser()
    resolved = str(path.resolve(strict=False))
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        return {"path": resolved, "sha256": None, "error": type(error).__name__}
    return {"path": resolved, "sha256": digest, "error": None}


def _ingest_config_identity(settings: Settings) -> dict[str, Any]:
    """Return all settings that can change make_extractor/store_extracted results."""
    retention = dataclasses.asdict(settings.retention_policy())
    retention["short_ttl_slots"] = sorted(retention["short_ttl_slots"])
    return {
        "extractor": {
            "mode": settings.extractor_mode,
            "version": LLM_EXTRACTOR_VERSION,
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "structured_mode": settings.llm_structured_mode,
            "thinking": settings.enable_llm_thinking,
            "schema_retries": settings.llm_schema_retries,
            "timeout": settings.llm_timeout,
            "max_attempts": settings.llm_max_attempts,
            "verification_mode": settings.verification_mode,
            "chunk_target_chars": settings.extraction_chunk_target_chars,
            "chunk_overlap_turns": settings.extraction_chunk_overlap_turns,
            "max_split_depth": settings.extraction_max_split_depth,
        },
        "embedding": {
            "mode": settings.embedder_mode,
            "base_url": settings.embedding_base_url,
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
            "api_mode": settings.embedding_api_mode,
            "text_type": settings.embedding_text_type,
            "connect_timeout": settings.embedding_connect_timeout,
            "read_timeout": settings.embedding_read_timeout,
            "max_attempts": settings.embedding_max_attempts,
        },
        "index": {
            "text_mode": settings.index_text_mode,
            "text_version": settings.index_text_version,
        },
        "retention": retention,
        "entity_alias_file": _entity_alias_file_identity(settings.entity_aliases_path),
        "relation_discovery": {
            "mode": settings.relation_discovery_mode,
            "pool_limit": settings.relation_discovery_pool_limit,
            "max_proposals": settings.relation_discovery_max_proposals,
            "auto_apply_confidence": settings.relation_auto_apply_confidence,
            "conflict_confidence": settings.relation_conflict_confidence,
        },
    }


def ingest_config_fingerprint(settings: Settings) -> str:
    """Hash every production ingest input used by the MemDaily cache."""
    canonical = json.dumps(
        _ingest_config_identity(settings),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cache_identity(traj: MemDailyTrajectory, settings: Settings) -> dict[str, Any]:
    """Return every ingest input that must match before a case DB is reusable."""
    return {
        "schema_version": 2,
        "case_id": traj.case_id,
        "case_fingerprint": _case_fingerprint(traj),
        "ingest_config_fingerprint": ingest_config_fingerprint(settings),
        "extractor_model": settings.llm_model,
        "extractor_version": LLM_EXTRACTOR_VERSION,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "embedding_api_mode": settings.embedding_api_mode,
        "embedding_text_type": settings.embedding_text_type,
        "index_text_mode": settings.index_text_mode,
        "index_text_version": settings.index_text_version,
    }


def _validate_cached_ingest(
    manifest_path: Path,
    traj: MemDailyTrajectory,
    settings: Settings,
    connection: Any | None = None,
) -> tuple[bool, str]:
    """Validate the manifest and, when open, every stored claim extractor version."""
    if not manifest_path.is_file():
        return False, "manifest_missing"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, "manifest_invalid"
    if not isinstance(manifest, Mapping):
        return False, "manifest_invalid"
    expected = _cache_identity(traj, settings)
    mismatches = sorted(key for key, value in expected.items() if manifest.get(key) != value)
    if mismatches:
        return False, f"manifest_mismatch:{','.join(mismatches)}"
    if connection is not None:
        versions = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT COALESCE(extractor_version,'<missing>') FROM claims"
            ).fetchall()
        }
        if versions and versions != {LLM_EXTRACTOR_VERSION}:
            return False, f"database_extractor_version:{','.join(sorted(versions))}"
    return True, "cache_valid"


def _remove_db_artifacts(db_path: Path) -> None:
    """Remove database and WAL/SHM files, asserting within benchmark dir."""
    root = DATABASE_ROOT.resolve()
    manifest_path = _case_manifest_path(db_path)
    for p in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        manifest_path,
        manifest_path.with_suffix(f"{manifest_path.suffix}.tmp"),
    ):
        if p.resolve().is_relative_to(root):
            p.unlink(missing_ok=True)


def _print_stale_cache_reason(traj: MemDailyTrajectory, db_path: Path, reason: str | None) -> None:
    print(
        f"{traj.case_id}: --skip-ingest cache stale reason={reason or 'unknown'}; " f"removing {db_path}",
        flush=True,
    )


def _ingest_trajectory(
    connection: Any,
    traj: MemDailyTrajectory,
    settings: Settings,
    embedder: Any,
    *,
    case_number: int,
    total: int,
) -> dict[str, Any]:
    """Ingest all messages of a trajectory into hl_mem with real LLM extraction."""
    service = IngestService(connection)
    extractor = make_extractor(settings, require_real=True, connection=connection)
    stats = {
        "messages": len(traj.messages),
        "extracted_claims": 0,
        "stored_claims": 0,
        "skipped_claims": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    started = time.perf_counter()

    for index, msg in enumerate(traj.messages, start=1):
        content = {
            "text": msg.text,
            "benchmark_locator": {
                "case_id": traj.case_id,
                "mid": msg.mid,
                "place": msg.place,
            },
        }
        event = {
            "id": msg.event_id,
            "idempotency_key": f"memdaily:{traj.case_id}:mid:{msg.mid}",
            "tenant_id": traj.namespace,
            "event_type": "message",
            "actor_type": "user",
            "content": content,
            "occurred_at": msg.occurred_at,
        }
        service.ingest_event(event)
        event["extractor"] = "llm"
        event["extractor_version"] = getattr(extractor, "extractor_version", LLM_EXTRACTOR_VERSION)
        claims = extractor.extract(
            content,
            {
                "actor_type": "user",
                "event_type": "message",
                "occurred_at": msg.occurred_at,
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
        if index == 1 or index % 5 == 0 or index == len(traj.messages):
            print(
                f"[{case_number}/{total}] {traj.case_id}: ingest {index}/{len(traj.messages)} "
                f"claims={stats['extracted_claims']}",
                flush=True,
            )

    stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return stats


# ─── Recall + QA ──────────────────────────────────────────────────────────────


def _decoded_value(value_json: object) -> str:
    text = str(value_json or "")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _claim_values(connection: Any) -> dict[str, str]:
    """Return {claim_id: value_text} for all claims."""
    rows = connection.execute("SELECT id,value_json FROM claims ORDER BY id").fetchall()
    return {str(row["id"]): _decoded_value(row["value_json"]) for row in rows}


def _claim_evidence_ids(connection: Any) -> dict[str, list[str]]:
    """Return {claim_id: [evidence_event_id, ...]} for all claims."""
    rows = connection.execute(
        "SELECT derived_id, evidence_id FROM evidence_links "
        "WHERE derived_type='claim' AND evidence_type='event' ORDER BY evidence_id"
    ).fetchall()
    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        result[str(row["derived_id"])].append(str(row["evidence_id"]))
    return dict(result)


def _recall_trajectory(
    connection: Any,
    traj: MemDailyTrajectory,
    settings: Settings,
    embedder: Any,
    reranker: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run recall for a trajectory question and return metrics + retrieved claims."""
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
        traj.question,
        limit=RECALL_K,
        as_of=traj.question_at,
        namespace=traj.namespace,
        debug=True,
    )
    raw_results = response.get("results") or []
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)]

    claim_values = _claim_values(connection)
    claim_evidence = _claim_evidence_ids(connection)

    retrieved_payload: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        claim_id = str(result.get("id") or "")
        evidence_ids = claim_evidence.get(claim_id, [])
        retrieved_payload.append(
            {
                "rank": rank,
                "claim_id": claim_id,
                "text": result.get("text") or claim_values.get(claim_id, ""),
                "value": claim_values.get(claim_id, ""),
                "score": result.get("score"),
                "evidence_event_ids": evidence_ids,
            }
        )

    # Recall@5: check if gold event IDs appear in top-5 claims' evidence
    recall_5 = score_recall_at_k(
        [item["evidence_event_ids"] for item in retrieved_payload],
        traj.gold_event_ids,
        k=5,
    )
    # Recall@10
    recall_10 = score_recall_at_k(
        [item["evidence_event_ids"] for item in retrieved_payload],
        traj.gold_event_ids,
        k=10,
    )

    metrics: dict[str, Any] = {
        "retrieved_claims": len(results),
        "recall_at_5": recall_5,
        "recall_at_10": recall_10,
        "gold_event_count": len(traj.gold_event_ids),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "answerability": response.get("answerability"),
    }
    return metrics, retrieved_payload


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


def _qa_dashscope_chat(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, int]:
    """Call DashScope compatible-mode chat completions via httpx.

    Returns (answer_text, total_tokens). Falls back gracefully on errors.
    Uses the standard /chat/completions endpoint (NOT the coding subdomain).
    """
    import httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    answer_text = ""
    choices = data.get("choices") or []
    if choices:
        answer_text = (choices[0].get("message") or {}).get("content") or ""
    total_tokens = (data.get("usage") or {}).get("total_tokens", 0)
    return answer_text, total_tokens


def _run_qa(
    connection: Any,
    traj: MemDailyTrajectory,
    retrieved: Sequence[Mapping[str, Any]],
    settings: Settings,
    *,
    answerability: Answerability = "supported",
) -> dict[str, Any]:
    """Ask the LLM to answer the question using retrieved claims.

    Uses a plain text chat completion (no StructuredOutputSpec) to avoid
    400 errors from structured output on the coding endpoint. The coding
    subdomain key works fine for plain text completions.
    """
    qa_model = os.environ.get("HL_MEM_EVAL_QA_MODEL", QA_FALLBACK_MODEL)
    refusal_kind = abstention_kind(answerability)
    if refusal_kind == "hard":
        predicted = "信息不足"
        scores = score_qa_accuracy(
            predicted,
            traj.answer,
            choices=traj.choices if traj.choices else None,
            ground_truth_choice=traj.ground_truth_choice,
        )
        return {
            "model": qa_model,
            "predicted_answer": predicted,
            "predicted_choice": "",
            "gold_answer": traj.answer,
            "ground_truth_choice": traj.ground_truth_choice,
            "exact_match": scores["exact_match"],
            "f1": scores["f1"],
            "choice_correct": scores["choice_correct"],
            "answerability": answerability,
            "abstention_kind": refusal_kind,
            "usage": {"total_tokens": 0},
        }

    # Resolve API key: prefer env override, then settings
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or settings.llm_api_key
    if not api_key:
        raise RuntimeError("QA answering requires LLM_API_KEY or DASHSCOPE_API_KEY " "in .env or environment")

    # Resolve base URL: default to the same endpoint as extraction (coding subdomain).
    # The coding key may not work on the standard DashScope endpoint.
    base_url = os.environ.get(
        "HL_MEM_EVAL_QA_BASE_URL",
        settings.llm_base_url or "https://coding.dashscope.aliyuncs.com/v1",
    )

    # Build context from retrieved claims
    context_lines = []
    for item in retrieved:
        rank = item.get("rank", "?")
        text = item.get("text") or item.get("value") or ""
        if text.strip():
            context_lines.append(f"[{rank}] {text.strip()}")
    context = "\n".join(context_lines)

    # If choices available, include them in the prompt
    choice_instruction = ""
    if traj.choices:
        choice_lines = "\n".join(f"  {k}. {v}" for k, v in sorted(traj.choices.items()))
        choice_instruction = (
            f"\n\n选择题选项:\n{choice_lines}\n" "请选择最合适的选项。回答格式：\n" "选项字母: <字母>\n答案: <答案内容>"
        )

    system_prompt = (
        "你是一个记忆问答助手。请根据提供的记忆片段回答问题。"
        "如果记忆中没有相关信息，请回答'信息不足'。"
        "回答要简洁，直接给出答案，不要解释。"
    )
    user_prompt = f"记忆片段:\n{context or '(无)'}\n\n" f"问题: {traj.question}{choice_instruction}"

    answer_text, total_tokens = _qa_dashscope_chat(api_key, base_url, qa_model, system_prompt, user_prompt)

    # Extract predicted answer and choice letter from text response
    predicted = answer_text.strip()
    predicted_choice = ""

    # Try to extract choice letter (e.g. "选项字母: A" or "A. xxx" at start)
    if traj.choices:
        # Pattern 1: explicit "选项字母: X"
        choice_match = re.search(r"选项字母[:：]\s*([A-Da-d])", predicted)
        if choice_match:
            predicted_choice = choice_match.group(1).upper()
        else:
            # Pattern 2: starts with a letter like "A. xxx" or "A、xxx"
            start_match = re.match(r"^([A-Da-d])[\.、\)）\.\s]", predicted)
            if start_match:
                predicted_choice = start_match.group(1).upper()

        # Clean up: if we found a choice letter, use the choice text for scoring
        if predicted_choice and predicted_choice in traj.choices:
            predicted = traj.choices[predicted_choice]

    # Score
    scores = score_qa_accuracy(
        predicted,
        traj.answer,
        choices=traj.choices if traj.choices else None,
        ground_truth_choice=traj.ground_truth_choice,
    )

    return {
        "model": qa_model,
        "predicted_answer": predicted,
        "predicted_choice": predicted_choice,
        "gold_answer": traj.answer,
        "ground_truth_choice": traj.ground_truth_choice,
        "exact_match": scores["exact_match"],
        "f1": scores["f1"],
        "choice_correct": scores["choice_correct"],
        "answerability": answerability,
        "abstention_kind": refusal_kind,
        "usage": {
            "total_tokens": total_tokens,
        },
    }


# ─── Case execution ───────────────────────────────────────────────────────────


def _run_case(
    traj: MemDailyTrajectory,
    settings: Settings,
    embedder: Any,
    reranker: Any,
    *,
    skip_ingest: bool,
    run_qa: bool,
    clean: bool,
    case_number: int,
    total: int,
) -> dict[str, Any]:
    """Execute full pipeline for one trajectory."""
    db_path = _case_db_path(traj.case_id)
    manifest_path = _case_manifest_path(db_path)
    result: dict[str, Any] = {
        "case_id": traj.case_id,
        "qtype": traj.qtype,
        "subtype": traj.subtype,
        "tid": traj.tid,
        "question": traj.question,
        "answer": traj.answer,
        "ground_truth_choice": traj.ground_truth_choice,
        "message_count": len(traj.messages),
        "gold_event_ids": list(traj.gold_event_ids),
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
        reuse_cache = False
        cache_reason: str | None = None
        existing_database = db_path.is_file()
        if skip_ingest and db_path.is_file():
            reuse_cache, cache_reason = _validate_cached_ingest(manifest_path, traj, settings)
        elif skip_ingest:
            cache_reason = "database_missing"
        if not reuse_cache:
            if skip_ingest and existing_database:
                _print_stale_cache_reason(traj, db_path, cache_reason)
            _remove_db_artifacts(db_path)

        database = Database(db_path, settings=settings)
        connection = database.open()

        if reuse_cache:
            reuse_cache, database_reason = _validate_cached_ingest(
                manifest_path,
                traj,
                settings,
                connection,
            )
            if not reuse_cache:
                cache_reason = database_reason
                database.close()
                database = None
                _print_stale_cache_reason(traj, db_path, cache_reason)
                _remove_db_artifacts(db_path)
                database = Database(db_path, settings=settings)
                connection = database.open()

        if reuse_cache:
            result["ingest"] = {
                "skipped": True,
                "cache_status": "reused",
                "cache_reason": "cache_valid",
                "cache_manifest": str(manifest_path),
            }
        else:
            ingest_result = _ingest_trajectory(
                connection,
                traj,
                settings,
                embedder,
                case_number=case_number,
                total=total,
            )
            ingest_result["cache_status"] = "stale_reingested" if skip_ingest else "fresh_ingest"
            ingest_result["cache_reason"] = cache_reason
            ingest_result["cache_manifest"] = str(manifest_path)
            result["ingest"] = ingest_result
            _write_json_atomic(manifest_path, _cache_identity(traj, settings))

        result["retrieval"], result["retrieved"] = _recall_trajectory(connection, traj, settings, embedder, reranker)

        if run_qa:
            result["qa"] = _run_qa(
                connection,
                traj,
                result["retrieved"],
                settings,
                answerability=result["retrieval"]["answerability"],
            )

    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if database is not None:
            database.close()
        if clean:
            import gc

            gc.collect()
            for attempt in range(3):
                try:
                    _remove_db_artifacts(db_path)
                    break
                except PermissionError:
                    if attempt < 2:
                        import time as _t

                        _t.sleep(0.5)
                    else:
                        pass  # leave it; --clean will get it next run
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


# ─── Aggregation & reporting ──────────────────────────────────────────────────


def _aggregate_group(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate metrics for a group of results (overall or per-type)."""
    successful = [r for r in results if not r.get("error")]
    qa_results = [qa for r in successful if isinstance((qa := r.get("qa")), Mapping)]
    retrieval_results = [r for r in successful if isinstance(r.get("retrieval"), Mapping)]

    recall_vals = [
        float(r["retrieval"]["recall_at_5"]) for r in retrieval_results if r["retrieval"].get("recall_at_5") is not None
    ]
    f1_vals = [float(qa["f1"]) for qa in qa_results if qa.get("f1") is not None]
    em_vals = [float(qa["exact_match"]) for qa in qa_results if qa.get("exact_match") is not None]
    choice_vals = [float(qa["choice_correct"]) for qa in qa_results if qa.get("choice_correct") is not None]

    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "failed_cases": len(results) - len(successful),
        "accuracy": mean(em_vals) if em_vals else None,
        "f1": round(mean(f1_vals), 4) if f1_vals else None,
        "recall_at_5": round(mean(recall_vals), 4) if recall_vals else None,
        "choice_accuracy": round(mean(choice_vals), 4) if choice_vals else None,
        "qa_evaluated_cases": len(qa_results),
    }


def aggregate_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate results into overall + per-qtype metrics."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in results:
        grouped[str(r.get("qtype") or "uncategorized")].append(r)
    return {
        "overall": _aggregate_group(results),
        "by_type": {qtype: _aggregate_group(items) for qtype, items in sorted(grouped.items())},
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _report(
    trajectories: Sequence[MemDailyTrajectory],
    results: Sequence[Mapping[str, Any]],
    settings: Settings,
    source: Path,
    started_at: str,
    status: str,
    skip_ingest: bool,
    run_qa: bool,
) -> dict[str, Any]:
    """Build the final report dict."""
    return {
        "schema_version": 1,
        "benchmark": "memdaily",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(source.resolve()),
            "bytes": source.stat().st_size if source.is_file() else None,
        },
        "run": {
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "total_trajectories": len(trajectories),
            "skip_ingest": skip_ingest,
            "qa_enabled": run_qa,
            "models": {
                "extractor": settings.llm_model,
                "extractor_version": LLM_EXTRACTOR_VERSION,
                "embedder": settings.embedding_model,
                "reranker": settings.reranker_model if settings.reranker_mode != "off" else "off",
            },
        },
        "metrics": aggregate_results(results),
        "cases": list(results),
    }


# ─── Markdown report ──────────────────────────────────────────────────────────


def _generate_markdown(report: Mapping[str, Any]) -> str:
    """Generate a human-readable Markdown summary from the JSON report."""
    metrics = report.get("metrics", {})
    overall = metrics.get("overall", {})
    by_type = metrics.get("by_type", {})
    run_info = report.get("run", {})

    lines: list[str] = []
    lines.append("# MemDaily Benchmark Report")
    lines.append("")
    lines.append(f"- **Benchmark**: {report.get('benchmark', 'memdaily')}")
    lines.append(f"- **Status**: {report.get('status', 'unknown')}")
    lines.append(f"- **Generated**: {report.get('generated_at', 'N/A')}")
    lines.append(f"- **Package version**: {run_info.get('package_version', 'N/A')}")
    lines.append(f"- **Total trajectories**: {run_info.get('total_trajectories', 'N/A')}")
    lines.append(f"- **Extractor**: {run_info.get('models', {}).get('extractor', 'N/A')}")
    lines.append(f"- **Embedder**: {run_info.get('models', {}).get('embedder', 'N/A')}")
    lines.append("")

    lines.append("## Overall Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for key in ("cases", "successful_cases", "failed_cases", "accuracy", "f1", "recall_at_5", "choice_accuracy"):
        val = overall.get(key)
        if val is None:
            val_str = "N/A"
        elif isinstance(val, float):
            val_str = f"{val:.4f}"
        else:
            val_str = str(val)
        lines.append(f"| {key} | {val_str} |")
    lines.append("")

    if by_type:
        lines.append("## Metrics by Question Type")
        lines.append("")
        lines.append("| Type | Cases | Accuracy | F1 | Recall@5 | Choice Acc |")
        lines.append("|------|-------|----------|----|---------:|------------|")
        for qtype, group in sorted(by_type.items()):
            acc = group.get("accuracy")
            f1 = group.get("f1")
            r5 = group.get("recall_at_5")
            ca = group.get("choice_accuracy")
            acc_s = f"{acc:.4f}" if acc is not None else "N/A"
            f1_s = f"{f1:.4f}" if f1 is not None else "N/A"
            r5_s = f"{r5:.4f}" if r5 is not None else "N/A"
            ca_s = f"{ca:.4f}" if ca is not None else "N/A"
            lines.append(f"| {qtype} | {group.get('cases', 0)} | " f"{acc_s} | {f1_s} | {r5_s} | {ca_s} |")
        lines.append("")

    cases = report.get("cases", [])
    if cases:
        lines.append("## Per-Case Results")
        lines.append("")
        lines.append("| # | Case ID | Type | Accuracy | F1 | Recall@5 | Error |")
        lines.append("|---|---------|------|----------|----|---------|-------|")
        for i, case in enumerate(cases, 1):
            qa = case.get("qa") or {}
            ret = case.get("retrieval") or {}
            acc = qa.get("exact_match") if qa else None
            f1 = qa.get("f1") if qa else None
            r5 = ret.get("recall_at_5") if ret else None
            err = case.get("error") or ""
            acc_s = f"{acc:.0f}" if isinstance(acc, (int, float, bool)) else "—"
            f1_s = f"{f1:.3f}" if isinstance(f1, (int, float)) else "—"
            r5_s = f"{r5:.3f}" if isinstance(r5, (int, float)) else "—"
            lines.append(
                f"| {i} | {case.get('case_id', '')} | {case.get('qtype', '')} | "
                f"{acc_s} | {f1_s} | {r5_s} | {'⚠️' if err else '✅'} |"
            )

    return "\n".join(lines) + "\n"


# ─── Main ─────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to memdaily.json",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--subset",
        default="events",
        help="MemDaily subtype (default: events)",
    )
    parser.add_argument(
        "--n-per-type",
        type=int,
        default=3,
        help="Max trajectories per question type (default: 3 for smoke test)",
    )
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--no-qa", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--qtypes",
        nargs="+",
        default=None,
        help="Filter to specific question types (e.g. --qtypes comparative). Default: all types.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.source.is_file():
        raise FileNotFoundError(f"MemDaily source not found: {args.source}")

    settings = load_settings(args.config, args.env_file)
    # Benchmark DBs are small — force sqlite_scan to avoid sqlite-vec dependency.
    settings = dataclasses.replace(settings, vector_backend="sqlite_scan")
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)

    trajectories = load_trajectories(
        args.source,
        subset=args.subset,
        n_per_type=args.n_per_type,
        qtypes=args.qtypes,
    )
    if not trajectories:
        raise ValueError(f"No trajectories found with subset={args.subset!r}")
    total = len(trajectories)
    started_at = datetime.now(timezone.utc).isoformat()
    run_qa = not args.no_qa

    print(
        f"MemDaily subset={args.subset} n_per_type={args.n_per_type} "
        f"total={total} extractor={settings.llm_model} "
        f"embedder={settings.embedding_model} qa={run_qa}",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    try:
        for case_number, traj in enumerate(trajectories, start=1):
            case_result = _run_case(
                traj,
                settings,
                embedder,
                reranker,
                skip_ingest=args.skip_ingest,
                run_qa=run_qa,
                clean=args.clean,
                case_number=case_number,
                total=total,
            )
            results.append(case_result)
            _write_json_atomic(
                args.output,
                _report(
                    trajectories,
                    results,
                    settings,
                    args.source,
                    started_at,
                    "running",
                    skip_ingest=args.skip_ingest,
                    run_qa=run_qa,
                ),
            )
            retrieval = case_result.get("retrieval") or {}
            qa = case_result.get("qa") or {}
            print(
                f"[{case_number}/{total}] {traj.case_id}: "
                f"R@5={retrieval.get('recall_at_5')} "
                f"F1={qa.get('f1')} "
                f"error={case_result.get('error')}",
                flush=True,
            )
    except Exception:
        if results:
            _write_json_atomic(
                args.output,
                _report(
                    trajectories,
                    results,
                    settings,
                    args.source,
                    started_at,
                    "aborted",
                    skip_ingest=args.skip_ingest,
                    run_qa=run_qa,
                ),
            )
        raise

    report = _report(
        trajectories,
        results,
        settings,
        args.source,
        started_at,
        "completed",
        skip_ingest=args.skip_ingest,
        run_qa=run_qa,
    )
    _write_json_atomic(args.output, report)

    # Generate Markdown report
    md_path = args.output.with_suffix(".md")
    md_path.write_text(_generate_markdown(report), encoding="utf-8")

    overall = report["metrics"]["overall"]
    print(
        f"completed cases={overall['cases']} failures={overall['failed_cases']} "
        f"accuracy={overall['accuracy']} f1={overall['f1']} "
        f"R@5={overall['recall_at_5']} output={args.output}",
        flush=True,
    )
    return 1 if overall["failed_cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
