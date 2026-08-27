"""Run the frozen compact==20 A/B with a shared root-response cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

EQUIPMENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = EQUIPMENT_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hl_mem.components import make_embedder, make_llm_client  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.core.vector import cosine_similarity  # noqa: E402
from hl_mem.domain.claims.dedup import is_safe_near_duplicate  # noqa: E402
from hl_mem.ingest.chunking import ChunkingPolicy  # noqa: E402
from hl_mem.ingest.llm_extractor import LLMExtractor  # noqa: E402
from hl_mem.llm.types import (  # noqa: E402
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
)
from hl_mem.observability.audit import audit_scope  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2  # noqa: E402

PROTOCOL_ID = "softsplit_ab_20260827_v1"
DELTA_REPAIR_PROTOCOL_ID = "softsplit_ab_20260827_v3"
MODEL = "qwen3.7-plus"
PROVIDER = "dashscope"
BASE_URL = "https://coding.dashscope.aliyuncs.com/v1"
DEFAULT_MANIFEST = EQUIPMENT_DIR / "manifest.json"
DEFAULT_OUTPUT = EQUIPMENT_DIR / "runs.jsonl"
DEFAULT_CONFIG = REPO_ROOT / "hl_mem.toml"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _request_payload(request: LLMRequest) -> dict[str, Any]:
    structured = request.structured_output
    return {
        "messages": [{"role": message.role, "content": message.content} for message in request.messages],
        "structured_output": (
            {
                "name": structured.name,
                "schema": structured.schema,
                "preferred_mode": structured.preferred_mode.value,
            }
            if structured is not None
            else None
        ),
    }


def _response_payload(response: LLMResponse) -> dict[str, Any]:
    return _json_safe(response)


def _request_fingerprint(request: LLMRequest) -> str:
    encoded = json.dumps(_request_payload(request), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _ResponseCache:
    def __init__(self) -> None:
        self._responses: dict[str, list[LLMResponse]] = defaultdict(list)
        self._replay_offsets: dict[str, int] = defaultdict(int)

    def record(self, fingerprint: str, response: LLMResponse) -> None:
        self._responses[fingerprint].append(response)

    def replay(self, fingerprint: str) -> LLMResponse | None:
        offset = self._replay_offsets[fingerprint]
        responses = self._responses.get(fingerprint, [])
        if offset >= len(responses):
            return None
        self._replay_offsets[fingerprint] += 1
        return responses[offset]


class _RecordingCachingClient:
    def __init__(self, client: Any, cache: _ResponseCache, *, replay: bool) -> None:
        self.client = client
        self.cache = cache
        self.replay_enabled = replay
        self.model = client.model
        self.provider = client.provider
        self.requests: list[dict[str, Any]] = []

    def complete(self, request: LLMRequest, *, timeout_seconds: float | None = None) -> LLMResponse:
        fingerprint = _request_fingerprint(request)
        started = time.perf_counter()
        cached = self.cache.replay(fingerprint) if self.replay_enabled else None
        try:
            if cached is not None:
                response = cached
                cache_hit = True
            else:
                response = (
                    self.client.complete(request)
                    if timeout_seconds is None
                    else self.client.complete(request, timeout_seconds=timeout_seconds)
                )
                cache_hit = False
                if not self.replay_enabled:
                    self.cache.record(fingerprint, response)
        except Exception as error:
            self.requests.append(
                {
                    "sequence": len(self.requests) + 1,
                    "fingerprint": fingerprint,
                    "cache_hit": False,
                    "status": "error",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "request": _request_payload(request),
                    "response": None,
                    "error": {
                        "class": type(error).__name__,
                        "message": str(error)[:500],
                    },
                }
            )
            raise
        self.requests.append(
            {
                "sequence": len(self.requests) + 1,
                "fingerprint": fingerprint,
                "cache_hit": cache_hit,
                "status": "success",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "request": _request_payload(request),
                "response": _response_payload(response),
                "error": None,
            }
        )
        return response


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, phase: str, action: str, outcome: str, *, detail=None, **dimensions: Any) -> bool:
        self.events.append(
            {
                "phase": phase,
                "action": action,
                "outcome": outcome,
                "detail": _json_safe(detail or {}),
                "dimensions": _json_safe(dimensions),
            }
        )
        return True


def _request_summary(requests: list[dict[str, Any]], expected_count: int) -> dict[str, int]:
    failed = sum(request["status"] == "error" for request in requests)
    missing_or_extra = abs(expected_count - len(requests))
    return {
        "expected_count": expected_count,
        "observed_count": len(requests),
        "failed_count": failed,
        "failed_or_missing_count": failed + missing_or_extra,
    }


def _run_arm(
    content: dict[str, Any] | str,
    context: dict[str, Any],
    client: _RecordingCachingClient,
    extractor_factory: Callable[[Any, bool, bool], Any],
    *,
    soft_split_enabled: bool,
    delta_repair_enabled: bool,
    expected_request_count: int,
) -> dict[str, Any]:
    audit = _RecordingAudit()
    extractor = extractor_factory(client, soft_split_enabled, delta_repair_enabled)
    claims: list[Any] = []
    error: dict[str, str] | None = None
    with audit_scope(audit):
        try:
            claims = extractor.extract(content, context)
        except Exception as caught:
            error = {"class": type(caught).__name__, "message": str(caught)[:500]}
    return {
        "error": error,
        "requests": client.requests,
        "request_summary": _request_summary(client.requests, expected_request_count),
        "claims": [_json_safe(claim) for claim in claims],
        "claim_count": len(claims),
        "audit_events": audit.events,
        "usage": {
            "input_tokens": int(getattr(extractor, "last_input_tokens", 0)),
            "output_tokens": int(getattr(extractor, "last_output_tokens", 0)),
            "total_tokens": int(getattr(extractor, "last_usage_tokens", 0)),
            "llm_call_count": int(getattr(extractor, "last_llm_call_count", len(client.requests))),
        },
    }


def _dedup_claim(claim: Mapping[str, Any], occurred_at: str) -> dict[str, Any]:
    return {
        "namespace_key": "default",
        "status": "active",
        "subject_entity_id": str(claim.get("subject") or ""),
        "predicate": str(claim.get("predicate") or ""),
        "canonical_slot": claim.get("canonical_slot"),
        "canonical_attribute": claim.get("canonical_attribute"),
        "value": claim.get("value"),
        "qualifiers": claim.get("qualifiers") or {},
        "valid_from": occurred_at,
        "valid_to": None,
    }


def duplicate_profile(
    claims: list[Mapping[str, Any]],
    embedder: Any,
    *,
    occurred_at: str,
    semantic_threshold: float,
) -> dict[str, Any]:
    if not claims:
        return {
            "claim_count": 0,
            "exact_duplicate_count": 0,
            "semantic_near_duplicate_count": 0,
            "duplicate_count": 0,
            "duplicate_rate": 0.0,
            "embedding_model": str(getattr(embedder, "model", "unknown")),
            "semantic_threshold": semantic_threshold,
        }
    prepared = [_dedup_claim(claim, occurred_at) for claim in claims]
    hashes = [compute_fact_hash_v2(item["subject_entity_id"], item["predicate"], item["value"]) for item in prepared]
    texts = [f"{item['subject_entity_id']} {item['predicate']} {item['value']}" for item in prepared]
    embeddings = embedder.embed_batch(texts)
    accepted: list[int] = []
    accepted_hashes: set[str] = set()
    exact_duplicates = 0
    semantic_duplicates = 0
    for index, (claim, fact_hash) in enumerate(zip(prepared, hashes, strict=True)):
        if fact_hash in accepted_hashes:
            exact_duplicates += 1
            continue
        near_duplicate = any(
            is_safe_near_duplicate(
                prepared[accepted_index],
                claim,
                similarity=cosine_similarity(embeddings[accepted_index], embeddings[index]),
                semantic_threshold=semantic_threshold,
            )
            for accepted_index in accepted
        )
        if near_duplicate:
            semantic_duplicates += 1
            continue
        accepted.append(index)
        accepted_hashes.add(fact_hash)
    duplicate_count = exact_duplicates + semantic_duplicates
    return {
        "claim_count": len(claims),
        "exact_duplicate_count": exact_duplicates,
        "semantic_near_duplicate_count": semantic_duplicates,
        "duplicate_count": duplicate_count,
        "duplicate_rate": duplicate_count / len(claims),
        "embedding_model": str(getattr(embedder, "model", "unknown")),
        "semantic_threshold": semantic_threshold,
    }


def _has_audit_outcome(arm: Mapping[str, Any], outcome: str) -> bool:
    return any(event.get("outcome") == outcome for event in arm["audit_events"])


def run_extraction_pair(
    case_id: str,
    content: dict[str, Any] | str,
    context: dict[str, Any],
    *,
    client_factory: Callable[[], Any],
    extractor_factory: Callable[[Any, bool, bool], Any],
    embedder: Any,
    semantic_threshold: float = 0.92,
) -> dict[str, Any]:
    """Run A once, replay its root response in B, then let B call both child requests."""
    cache = _ResponseCache()
    control_client = _RecordingCachingClient(client_factory(), cache, replay=False)
    control = _run_arm(
        content,
        context,
        control_client,
        extractor_factory,
        soft_split_enabled=False,
        delta_repair_enabled=False,
        expected_request_count=1,
    )
    treatment_client = _RecordingCachingClient(client_factory(), cache, replay=True)
    treatment = _run_arm(
        content,
        context,
        treatment_client,
        extractor_factory,
        soft_split_enabled=True,
        delta_repair_enabled=False,
        expected_request_count=3,
    )
    occurred_at = str(context.get("occurred_at") or "2026-08-27T00:00:00Z")
    profile_error: dict[str, str] | None = None
    try:
        control["duplicate_profile"] = duplicate_profile(
            control["claims"],
            embedder,
            occurred_at=occurred_at,
            semantic_threshold=semantic_threshold,
        )
        treatment["duplicate_profile"] = duplicate_profile(
            treatment["claims"],
            embedder,
            occurred_at=occurred_at,
            semantic_threshold=semantic_threshold,
        )
    except Exception as error:
        profile_error = {"class": type(error).__name__, "message": str(error)[:500]}
        empty_profile = {
            "claim_count": 0,
            "exact_duplicate_count": 0,
            "semantic_near_duplicate_count": 0,
            "duplicate_count": 0,
            "duplicate_rate": 0.0,
            "error": profile_error,
        }
        control["duplicate_profile"] = dict(empty_profile)
        treatment["duplicate_profile"] = dict(empty_profile)

    control_exact_twenty = _has_audit_outcome(control, "claim_limit_reached")
    split_applied = _has_audit_outcome(treatment, "claim_limit_split_applied")
    net_new = max(0, treatment["claim_count"] - control["claim_count"])
    protocol_errors: list[str] = []
    if control["error"] is not None:
        protocol_errors.append("control_error")
    if treatment["error"] is not None:
        protocol_errors.append("treatment_error")
    if not control_exact_twenty:
        protocol_errors.append("control_root_not_compact_exact_20")
    if not split_applied:
        protocol_errors.append("treatment_soft_split_not_applied")
    if control["request_summary"]["observed_count"] != 1:
        protocol_errors.append("control_request_count_not_1")
    if treatment["request_summary"]["observed_count"] != 3:
        protocol_errors.append("treatment_request_count_not_3")
    if profile_error is not None:
        protocol_errors.append("duplicate_profile_error")
    return {
        "protocol_id": PROTOCOL_ID,
        "case_id": case_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "success" if not protocol_errors else "failed",
        "failure_reasons": protocol_errors,
        "control": control,
        "treatment": treatment,
        "comparison": {
            "net_new_after_split": net_new,
            "control_root_compact_exact_20": control_exact_twenty,
            "treatment_soft_split_applied": split_applied,
            "duplicate_rate_delta_pp": (
                treatment["duplicate_profile"]["duplicate_rate"] - control["duplicate_profile"]["duplicate_rate"]
            )
            * 100,
        },
    }


def run_delta_repair_case(
    case_id: str,
    content: dict[str, Any] | str,
    context: dict[str, Any],
    *,
    client_factory: Callable[[], Any],
    extractor_factory: Callable[[Any, bool, bool], Any],
    embedder: Any,
    semantic_threshold: float = 0.92,
) -> dict[str, Any]:
    """Run only the real P0+P1 arm; v3 scoring loads its P0 baseline separately."""
    treatment_client = _RecordingCachingClient(client_factory(), _ResponseCache(), replay=False)
    treatment = _run_arm(
        content,
        context,
        treatment_client,
        extractor_factory,
        soft_split_enabled=True,
        delta_repair_enabled=True,
        expected_request_count=0,
    )
    audit_events = treatment["audit_events"]
    split_applied = _has_audit_outcome(treatment, "claim_limit_split_applied")
    repair_events = [event for event in audit_events if event.get("outcome") == "delta_repair_applied"]
    residual_after_repair = sum(event.get("outcome") == "claim_limit_residual_after_repair" for event in audit_events)
    expected_requests = 1 + (2 if split_applied else 0) + len(repair_events)
    treatment["request_summary"] = _request_summary(treatment["requests"], expected_requests)
    occurred_at = str(context.get("occurred_at") or "2026-08-27T00:00:00Z")
    profile_error: dict[str, str] | None = None
    try:
        treatment["duplicate_profile"] = duplicate_profile(
            treatment["claims"],
            embedder,
            occurred_at=occurred_at,
            semantic_threshold=semantic_threshold,
        )
    except Exception as error:
        profile_error = {"class": type(error).__name__, "message": str(error)[:500]}
        treatment["duplicate_profile"] = {
            "claim_count": 0,
            "exact_duplicate_count": 0,
            "semantic_near_duplicate_count": 0,
            "duplicate_count": 0,
            "duplicate_rate": 0.0,
            "error": profile_error,
        }
    protocol_errors: list[str] = []
    if treatment["error"] is not None:
        protocol_errors.append("treatment_error")
    if not _has_audit_outcome(treatment, "claim_limit_reached"):
        protocol_errors.append("treatment_root_not_compact_exact_20")
    if not split_applied:
        protocol_errors.append("treatment_soft_split_not_applied")
    if treatment["request_summary"]["observed_count"] != expected_requests:
        protocol_errors.append("treatment_request_count_mismatch")
    if profile_error is not None:
        protocol_errors.append("duplicate_profile_error")
    return {
        "protocol_id": DELTA_REPAIR_PROTOCOL_ID,
        "case_id": case_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "success" if not protocol_errors else "failed",
        "failure_reasons": protocol_errors,
        "treatment": treatment,
        "comparison": {
            "treatment_soft_split_applied": split_applied,
            "delta_repair_applied_count": len(repair_events),
            "net_new_after_repair": sum(
                max(0, int(event.get("detail", {}).get("net_new_after_repair", 0))) for event in repair_events
            ),
            "residual_after_repair_count": residual_after_repair,
        },
    }


def _open_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _event_text(content: Any) -> str:
    if isinstance(content, dict) and "text" in content:
        return str(content["text"])
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _load_case_payload(database_path: Path, case: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source_ids = case.get("source_event_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError(f"case {case.get('case_id')} has no source_event_ids")
    expected_hashes = {
        str(source["event_id"]): str(source["content_sha256"])
        for source in case.get("sources", [])
        if isinstance(source, Mapping)
    }
    with _open_read_only(database_path) as connection:
        sources: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for index, event_id in enumerate(source_ids):
            row = connection.execute("SELECT * FROM events WHERE id=?", (str(event_id),)).fetchone()
            if row is None:
                raise ValueError(f"source event is missing: {event_id}")
            raw = dict(row)
            content_json = str(raw["content_json"])
            actual_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            if expected_hashes.get(str(event_id)) != actual_hash:
                raise ValueError(f"source content hash changed: {event_id}")
            content = json.loads(content_json)
            metadata_raw = raw.get("metadata_json")
            metadata = json.loads(metadata_raw) if metadata_raw else {}
            turn = metadata.get("turn_id", metadata.get("turn_index", index)) if isinstance(metadata, dict) else index
            sources.append({**raw, "event_index": index, "turn": turn, "content": content})
            messages.append(
                {
                    "event_index": index,
                    "speaker": str(raw.get("actor_type") or "unknown"),
                    "turn": turn,
                    "occurred_at": raw.get("occurred_at"),
                    "content": _event_text(content),
                }
            )
    anchor = sources[0]
    return (
        {"messages": messages},
        {
            "occurred_at": anchor.get("occurred_at"),
            "actor_type": "conversation",
            "event_type": "message",
            "session_id": anchor.get("session_id"),
            "recent_events": [],
            "_source_events": sources,
        },
    )


def _extractor_factory(settings: Settings) -> Callable[[Any, bool, bool], LLMExtractor]:
    structured_mode = (
        StructuredOutputMode.JSON_OBJECT
        if settings.llm_structured_mode == "json_object"
        else StructuredOutputMode.JSON_SCHEMA
    )

    def build(client: Any, soft_split_enabled: bool, delta_repair_enabled: bool) -> LLMExtractor:
        return LLMExtractor(
            client,
            ChunkingPolicy(
                target_chars=settings.extraction_chunk_target_chars,
                overlap_turns=settings.extraction_chunk_overlap_turns,
                max_split_depth=settings.extraction_max_split_depth,
            ),
            schema_retries=settings.llm_schema_retries,
            structured_mode=structured_mode,
            soft_split_enabled=soft_split_enabled,
            delta_repair_enabled=delta_repair_enabled,
            verifier=None,
            verification_mode="off",
            lesson_signal_mode=settings.lesson_signal_mode,
        )

    return build


def _load_completed_case_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    case_ids: set[str] = set()
    for line_number, line in enumerate(output_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid JSONL at line {line_number}") from error
        case_ids.add(str(record["case_id"]))
    return case_ids


def run_manifest(
    manifest_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    database_path: Path | None = None,
    concurrency: int = 8,
    delta_repair: bool = False,
) -> dict[str, int]:
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("protocol_id") != PROTOCOL_ID:
        raise ValueError("manifest protocol_id does not match")
    resolved_database = database_path or Path(str(manifest["source_database"]))
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != int(manifest.get("case_count", -1)):
        raise ValueError("manifest case_count does not match cases")
    if settings.embedder_mode != "real":
        raise ValueError("frozen duplicate gate requires embedding.mode='real'")
    completed = _load_completed_case_ids(output_path)
    pending = [case for case in cases if str(case["case_id"]) not in completed]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    embedder = make_embedder(settings)
    build_extractor = _extractor_factory(settings)

    def evaluate(case: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(case["case_id"])
        try:
            content, context = _load_case_payload(resolved_database, case)
            run_case = run_delta_repair_case if delta_repair else run_extraction_pair
            record = run_case(
                case_id,
                content,
                context,
                client_factory=lambda: make_llm_client(settings, operation="extract"),
                extractor_factory=build_extractor,
                embedder=embedder,
                semantic_threshold=settings.dedup_threshold,
            )
        except Exception as error:
            record = {
                "protocol_id": DELTA_REPAIR_PROTOCOL_ID if delta_repair else PROTOCOL_ID,
                "case_id": case_id,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "failure_reasons": ["runner_error"],
                "error": {"class": type(error).__name__, "message": str(error)[:500]},
            }
        record["configuration"] = {
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_base_url": settings.llm_base_url,
            "enable_llm_thinking": settings.enable_llm_thinking,
            "max_concurrency": concurrency,
            "embedding_model": settings.embedding_model,
            "dedup_threshold": settings.dedup_threshold,
            "delta_repair_enabled": delta_repair,
        }
        return record

    written = 0
    with output_path.open("a", encoding="utf-8", newline="\n") as stream:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(evaluate, case): str(case["case_id"]) for case in pending}
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                written += 1
    return {"manifest_cases": len(cases), "already_completed": len(completed), "written": written}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--delta-repair", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    loaded = load_settings(args.config, args.env_file)
    if args.delta_repair:
        settings = replace(
            loaded,
            extractor_mode="llm",
            verification_mode="off",
            enable_llm_thinking=False,
            extraction_soft_split_enabled=True,
            extraction_delta_repair_enabled=True,
        )
    else:
        settings = replace(
            loaded,
            extractor_mode="llm",
            verification_mode="off",
            llm_provider=PROVIDER,
            llm_model=MODEL,
            llm_base_url=BASE_URL,
            enable_llm_thinking=False,
            extraction_soft_split_enabled=False,
            extraction_delta_repair_enabled=False,
        )
    settings.validate()
    result = run_manifest(
        args.manifest,
        args.output,
        settings,
        database_path=args.database,
        concurrency=args.concurrency,
        delta_repair=args.delta_repair,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
