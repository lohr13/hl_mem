"""C-series 真实 RecallService 执行与实验 packet 组装。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from hl_mem.application.context_packet import normalize_relation_components, render_memory_text
from hl_mem.application.recall import RecallService
from hl_mem.evaluation.c_series import (
    PACKET_CLAIM_LIMIT,
    PACKET_TOKEN_BUDGET,
    arm_spec,
    atomic_pack,
    relation_multihop_intent_v1,
    select_raw_events,
)
from hl_mem.protocols import EmbedderProtocol, RerankerProtocol
from hl_mem.settings import Settings, VectorBackend
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

_LEAK_KEYS = frozenset(
    {
        "gold",
        "answer",
        "answers",
        "answer_entities",
        "role_action_object",
        "forbidden_entities",
        "forbidden_assertions",
        "accepted_rubrics",
        "rubrics",
        "verdict",
    }
)


@dataclass(frozen=True)
class RecallExecution:
    packet: tuple[dict[str, Any], ...]
    seed_packet: tuple[dict[str, Any], ...]
    answerability: str
    search_trace: dict[str, Any]
    recall_latency_seconds: float
    relation_paths: tuple[dict[str, Any], ...]


def frozen_runtime_settings(settings: Settings) -> Settings:
    """返回实验专用冻结配置，不修改全局/生产 Settings。"""
    runtime = dataclasses.replace(
        settings,
        vector_backend=VectorBackend.SQLITE_SCAN,
        query_expansion_mode="off",
        relation_discovery_mode="off",
        packed_context_token_budget=PACKET_TOKEN_BUDGET,
        recall_candidate_floor=max(50, settings.recall_candidate_floor),
        tag_channel_enabled=False,
        recall_side_effect_backoff_seconds=0.0,
    )
    runtime.validate()
    return runtime


def assert_gold_free(payload: Any, *, path: str = "$") -> None:
    """递归拒绝 runner 输入或 prompt payload 中的评分字段。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            normalized = str(key).casefold()
            if (
                normalized in _LEAK_KEYS
                or any(token in normalized for token in ("gold", "forbidden", "rubric"))
                or normalized.endswith(("_gold", "_verdict", "_answer_ref"))
            ):
                raise ValueError(f"gold/scorer field forbidden at {path}.{key}")
            assert_gold_free(value, path=f"{path}.{key}")
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for index, value in enumerate(payload):
            assert_gold_free(value, path=f"{path}[{index}]")


def _backup_connection(path: Path) -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    try:
        source.backup(target)
    finally:
        source.close()
    return target


def _claim_payload(
    repo: ClaimRepository,
    event_repo: EventRepository,
    claim_id: str,
    *,
    result: Mapping[str, Any] | None,
    trace_candidate: Mapping[str, Any] | None,
    seed_rank_by_id: Mapping[str, int | None],
    source_cache: Mapping[str, str],
    source_corpora: Sequence[Mapping[str, str]],
) -> dict[str, Any] | None:
    claim = repo.get_claim(claim_id)
    if claim is None:
        return None
    raw_qualifiers = claim.get("qualifiers")
    qualifiers: Mapping[str, Any] = raw_qualifiers if isinstance(raw_qualifiers, Mapping) else {}
    relation_paths = trace_candidate.get("relation_paths") if isinstance(trace_candidate, Mapping) else []
    expanded_ranks = sorted(
        {
            rank
            for path in relation_paths or []
            if isinstance(path, Mapping)
            and isinstance((rank := seed_rank_by_id.get(str(path.get("seed_id") or ""))), int)
        }
    )
    text = str((result or {}).get("text") or claim.get("index_text") or claim.get("value") or "")
    final_rank = (trace_candidate or {}).get("final_rank")
    pre_rank = (trace_candidate or {}).get("pre_rank")
    evidence_event_ids = [
        str(item.get("id"))
        for item in (result or {}).get("evidence") or []
        if isinstance(item, Mapping) and item.get("type") == "event" and item.get("id")
    ]
    if not evidence_event_ids:
        evidence_event_ids = [
            str(row["evidence_id"])
            for row in repo.connection.execute(
                "SELECT evidence_id FROM evidence_links "
                "WHERE derived_type='claim' AND derived_id=? AND evidence_type='event' ORDER BY id",
                (claim_id,),
            ).fetchall()
        ]
    provenance = [
        _event_provenance(event, source_cache, source_corpora)
        for event_id in evidence_event_ids
        if (event := event_repo.get_event(event_id)) is not None
    ]
    value = claim.get("value")
    object_fallback = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    relation = normalize_relation_components(
        qualifiers.get("role") or claim.get("subject_entity_id"),
        qualifiers.get("action") or claim.get("predicate"),
        qualifiers.get("object") or object_fallback,
    )
    role, action, object_ = relation or ("", "", "")
    rendered_text = render_memory_text(text, role=role, action=action, object_=object_)
    return {
        "claim_id": claim_id,
        "text": text,
        "entities": [str(item) for item in claim.get("entities") or []],
        "role": role,
        "action": action,
        "object": object_,
        "rendered_text": rendered_text,
        "slot": str(claim.get("canonical_slot") or ""),
        "evidence_event_ids": evidence_event_ids,
        "evidence_provenance": provenance,
        "rank": int(final_rank or pre_rank or 10**6),
        "seed_rank": int(pre_rank) if isinstance(pre_rank, int) and not relation_paths else None,
        "expanded_from_seed_ranks": expanded_ranks,
        "token_count": max(1, (len(rendered_text) + 1) // 2),
    }


def render_packet_context(
    packet: Sequence[Mapping[str, Any]],
    *,
    structured: bool = True,
    empty: str = "",
) -> str:
    """按实验 packet 顺序渲染 reader 输入，可显式复现旧版纯文本表示。"""

    lines: list[str] = []
    for index, item in enumerate(packet, start=1):
        text = str(item.get("text") or "")
        if not text:
            continue
        visible = (
            str(item.get("rendered_text") or "")
            or render_memory_text(
                text,
                role=item.get("role"),
                action=item.get("action"),
                object_=item.get("object"),
            )
            if structured
            else text
        )
        lines.append(f"[{index}] {visible}")
    return "\n".join(lines) or empty


def _event_modality(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("event_type") or "").casefold()
    source_uri = str(event.get("source_uri") or "").casefold().split("?", 1)[0]
    if "image" in event_type:
        return "image"
    if "audio" in event_type or "voice" in event_type:
        return "audio"
    if source_uri.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")):
        return "image"
    if source_uri.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
        return "audio"

    def content_modalities(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized_key = str(key).casefold()
                normalized_value = str(item).casefold() if isinstance(item, str) else ""
                if "image" in normalized_key or normalized_value in {"image", "image_url"}:
                    result.add("image")
                if any(token in normalized_key for token in ("audio", "voice")) or normalized_value in {
                    "audio",
                    "voice",
                }:
                    result.add("audio")
                result.update(content_modalities(item))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                result.update(content_modalities(item))
        return result

    modalities = content_modalities(event.get("content"))
    if "image" in modalities:
        return "image"
    if "audio" in modalities:
        return "audio"
    return "text"


def _event_provenance(
    event: Mapping[str, Any],
    source_cache: Mapping[str, str],
    source_corpora: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "event_id": str(event.get("id") or event.get("event_id") or ""),
        "namespace": str(event.get("tenant_id") or ""),
        "occurred_at": str(event.get("occurred_at") or ""),
        "recorded_at": str(event.get("recorded_at") or ""),
        "modality": _event_modality(event),
        "content_kind": str(event.get("event_type") or "unknown"),
        "source_cache_identity": source_cache["identity"],
        "source_cache_sha256": source_cache["sha256"],
        "source_corpora": [dict(item) for item in source_corpora],
    }


def _source_metadata(case: Mapping[str, Any], db_path: Path) -> tuple[dict[str, str], tuple[dict[str, str], ...]]:
    identity = str(db_path.resolve())
    configured_identity = case.get("source_cache_identity")
    if configured_identity is not None and Path(str(configured_identity)).resolve() != db_path.resolve():
        raise ValueError("source cache identity does not match DB path")
    configured_sha = str(case.get("source_cache_sha256") or "")
    cache_sha = configured_sha or hashlib.sha256(db_path.read_bytes()).hexdigest()
    raw_corpora = case.get("source_corpora") or []
    corpora = tuple(
        {"id": str(item["id"]), "sha256": str(item["sha256"])}
        for item in raw_corpora
        if isinstance(item, Mapping) and item.get("id") and item.get("sha256")
    )
    return {"identity": identity, "sha256": cache_sha}, corpora


def _path_rows(trace_candidates: Mapping[str, Any]) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for candidate in trace_candidates.values():
        if not isinstance(candidate, Mapping):
            continue
        for metadata in candidate.get("relation_paths") or []:
            if not isinstance(metadata, Mapping):
                continue
            hops = metadata.get("path") or []
            if not hops:
                continue
            ids = [str(hops[0]["from_id"]), *(str(hop["to_id"]) for hop in hops)]
            paths.append(
                {
                    "claim_ids": ids,
                    "expansion_score": float(metadata.get("expansion_score") or 0.0),
                }
            )
    return paths


def _path_covers_mask(path: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]], required: Sequence[str]) -> bool:
    members = [by_id.get(str(claim_id)) for claim_id in path.get("claim_ids") or []]
    return all(any(member and str(member.get(component) or "") for member in members) for component in required)


def _pack_ranked(items: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    packed: list[dict[str, Any]] = []
    used = 0
    for raw in items:
        item = dict(raw)
        tokens = int(item.get("token_count") or 0)
        if len(packed) >= PACKET_CLAIM_LIMIT or used + tokens > PACKET_TOKEN_BUDGET:
            continue
        packed.append(item)
        used += tokens
    return tuple(packed)


def _execute_on_connection(
    connection: sqlite3.Connection,
    case: Mapping[str, Any],
    settings: Settings,
    embedder: EmbedderProtocol,
    reranker: RerankerProtocol | None,
    *,
    arm_id: str,
    query: str | None = None,
    max_depth_override: int | None = None,
    source_cache: Mapping[str, str],
    source_corpora: Sequence[Mapping[str, str]],
) -> RecallExecution:
    question = str(query or case["question"])
    intent = relation_multihop_intent_v1(question)
    spec = arm_spec(arm_id)
    relation_config = spec.relation_config(intent_eligible=intent.eligible)
    if max_depth_override is not None:
        relation_config = dataclasses.replace(relation_config, max_depth=max_depth_override)
    service = RecallService(connection, embedder, reranker, relation_config, settings, query_expander=None)
    started = time.perf_counter()
    response = service.recall(
        question,
        limit=PACKET_CLAIM_LIMIT,
        as_of=case.get("question_at"),
        known_as_of=case.get("known_as_of"),
        namespace=str(case["namespace"]),
        debug=True,
        token_budget=PACKET_TOKEN_BUDGET,
        context_mode="packed",
    )
    latency = time.perf_counter() - started
    raw_trace = response.get("search_trace")
    trace: Mapping[str, Any] = raw_trace if isinstance(raw_trace, Mapping) else {}
    raw_candidates = trace.get("candidates")
    trace_candidates: Mapping[str, Any] = raw_candidates if isinstance(raw_candidates, Mapping) else {}
    seed_rank_by_id = {
        str(claim_id): candidate.get("pre_rank") if isinstance(candidate, Mapping) else None
        for claim_id, candidate in trace_candidates.items()
    }
    results = {
        str(item.get("id")): item
        for item in response.get("results") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    paths = _path_rows(trace_candidates)
    relevant_ids = list(results)
    relevant_ids.extend(
        str(claim_id)
        for claim_id, candidate in trace_candidates.items()
        if isinstance(candidate, Mapping)
        and isinstance(candidate.get("pre_rank"), int)
        and int(candidate["pre_rank"]) <= 5
    )
    for path in paths:
        relevant_ids.extend(str(item) for item in path["claim_ids"])
    repo = ClaimRepository(connection, settings=settings)
    event_repo = EventRepository(connection, settings)
    items_by_id: dict[str, dict[str, Any]] = {}
    for claim_id in dict.fromkeys(relevant_ids):
        item = _claim_payload(
            repo,
            event_repo,
            claim_id,
            result=results.get(claim_id),
            trace_candidate=trace_candidates.get(claim_id),
            seed_rank_by_id=seed_rank_by_id,
            source_cache=source_cache,
            source_corpora=source_corpora,
        )
        if item is not None:
            items_by_id[claim_id] = item
    ranked = sorted(
        (items_by_id[claim_id] for claim_id in results if claim_id in items_by_id),
        key=lambda item: (int(item["rank"]), str(item["claim_id"])),
    )
    if spec.atomic_path_packing:
        eligible_paths = [path for path in paths if _path_covers_mask(path, items_by_id, intent.required_rao)]
        candidate_pool = [*ranked, *(item for item in items_by_id.values() if item not in ranked)]
        packet = atomic_pack(candidate_pool, eligible_paths).items
    else:
        packet = _pack_ranked(ranked)
    seed_items: list[tuple[int, dict[str, Any]]] = []
    for claim_id, item in items_by_id.items():
        seed_rank = seed_rank_by_id.get(claim_id)
        if isinstance(seed_rank, int) and seed_rank <= 5:
            seed_items.append((seed_rank, item))
    seed_packet = tuple(
        dict(item) for _, item in sorted(seed_items, key=lambda pair: (pair[0], str(pair[1]["claim_id"])))[:5]
    )
    return RecallExecution(
        packet=tuple(dict(item) for item in packet),
        seed_packet=seed_packet,
        answerability=str(response.get("answerability") or "no_evidence"),
        search_trace=dict(trace),
        recall_latency_seconds=latency,
        relation_paths=tuple(paths),
    )


def recall_visible_case(
    case: Mapping[str, Any],
    settings: Settings,
    embedder: EmbedderProtocol,
    reranker: RerankerProtocol | None,
    *,
    db_path: Path,
    arm_id: str,
    query: str | None = None,
    max_depth_override: int | None = None,
) -> RecallExecution:
    """在 source DB 的内存 backup 上真实执行 RecallService，避免污染冻结 cache。"""
    assert_gold_free(case)
    runtime = frozen_runtime_settings(settings)
    source_cache, source_corpora = _source_metadata(case, db_path)
    connection = _backup_connection(db_path)
    try:
        return _execute_on_connection(
            connection,
            case,
            runtime,
            embedder,
            reranker,
            arm_id=arm_id,
            query=query,
            max_depth_override=max_depth_override,
            source_cache=source_cache,
            source_corpora=source_corpora,
        )
    finally:
        connection.close()


def execute_raw_rescue(
    db_path: Path,
    case: Mapping[str, Any],
    base: RecallExecution,
    *,
    query: str,
    settings: Settings,
) -> tuple[dict[str, Any], ...]:
    """在真实 FTS/可见性结果上用 raw 替换 packet 尾部，保持总项不超过十。"""
    del settings  # visibility is entirely encoded in case and DB rows
    source_cache, source_corpora = _source_metadata(case, db_path)
    linked = [event_id for item in base.packet for event_id in item.get("evidence_event_ids") or []]
    connection = _backup_connection(db_path)
    try:
        raw = select_raw_events(
            connection,
            query=query,
            namespace=str(case["namespace"]),
            question_at=case.get("question_at"),
            known_as_of=case.get("known_as_of"),
            linked_event_ids=linked,
        )
    finally:
        connection.close()
    raw_items = [
        {
            **item,
            "kind": "raw_event",
            "claim_id": f"raw:{item['event_id']}",
            "entities": [],
            "evidence_event_ids": [item["event_id"]],
            "evidence_provenance": [_event_provenance(item, source_cache, source_corpora)],
        }
        for item in raw
    ]
    raw_items = raw_items[:PACKET_CLAIM_LIMIT]
    claim_limit = PACKET_CLAIM_LIMIT - len(raw_items)
    raw_tokens = sum(int(item["token_count"]) for item in raw_items)
    claim_budget = PACKET_TOKEN_BUDGET - raw_tokens
    claims: list[dict[str, Any]] = []
    used = 0
    for raw_claim in base.packet:
        tokens = int(raw_claim["token_count"])
        if len(claims) >= claim_limit or used + tokens > claim_budget:
            continue
        claims.append(dict(raw_claim))
        used += tokens
    return tuple([*claims, *raw_items])


def execute_planner_subgoals(
    db_path: Path,
    case: Mapping[str, Any],
    base: RecallExecution,
    subgoals: Sequence[Mapping[str, Any]],
    *,
    settings: Settings,
    embedder: EmbedderProtocol,
    reranker: RerankerProtocol | None,
) -> tuple[dict[str, Any], ...]:
    """执行最多两个真实受限 recall，再与 C4 packet 去重合并并重新预算。"""
    merged = [dict(item) for item in base.packet]
    seen = {str(item["claim_id"]) for item in merged}
    for subgoal in subgoals[:2]:
        recalled = recall_visible_case(
            case,
            settings,
            embedder,
            reranker,
            db_path=db_path,
            arm_id="C3",
            query=str(subgoal["query"]),
            max_depth_override=int(subgoal["max_depth"]),
        )
        for item in recalled.packet:
            if str(item["claim_id"]) not in seen:
                merged.append(dict(item))
                seen.add(str(item["claim_id"]))
    return _pack_ranked(merged)


def materialize_visible_case(
    db_path: Path,
    case: Mapping[str, Any],
    settings: Settings,
    *,
    embedder: EmbedderProtocol | None = None,
) -> None:
    """把可见 fixture 物化为可重复 SQLite cache；不读取其 gold 字段。"""
    safe_case = {key: value for key, value in case.items() if key not in _LEAK_KEYS}
    assert_gold_free(safe_case)
    runtime = frozen_runtime_settings(settings)
    database = Database(db_path, settings=runtime)
    try:
        with database.connect() as connection:
            events = safe_case.get("events") or []
            evidence_ids = list(
                dict.fromkeys(
                    str(event_id)
                    for claim in safe_case.get("claims") or []
                    for event_id in claim.get("evidence_event_ids") or []
                )
            )
            for index, raw in enumerate(events, start=1):
                event = (
                    dict(raw)
                    if isinstance(raw, Mapping)
                    else {
                        "event_id": (
                            evidence_ids[index - 1]
                            if index - 1 < len(evidence_ids)
                            else f"{safe_case['case_id']}:event:{index}"
                        ),
                        "text": str(raw),
                    }
                )
                EventRepository(connection, runtime).insert_event(
                    {
                        "id": str(event.get("event_id") or f"{safe_case['case_id']}:event:{index}"),
                        "tenant_id": str(event.get("tenant_id") or safe_case["namespace"]),
                        "event_type": "message",
                        "actor_type": "user",
                        "content": {"text": str(event.get("text") or "")},
                        "occurred_at": str(event.get("occurred_at") or "2026-01-01T00:00:00+00:00"),
                        "recorded_at": str(
                            event.get("recorded_at") or event.get("occurred_at") or "2026-01-01T00:00:00+00:00"
                        ),
                    }
                )
            repo = ClaimRepository(connection, settings=runtime)
            for raw in safe_case.get("claims") or []:
                text = str(raw["text"])
                is_seed = int(raw.get("rank") or 10**6) <= 5
                claim = {
                    "id": str(raw["claim_id"]),
                    "namespace_key": str(safe_case["namespace"]),
                    "subject_entity_id": str(raw.get("role") or "subject"),
                    "predicate": str(raw.get("action") or "fact"),
                    "value": text,
                    "qualifiers": {
                        "role": str(raw.get("role") or ""),
                        "action": str(raw.get("action") or ""),
                        "object": str(raw.get("object") or ""),
                    },
                    "entities_json": json.dumps(raw.get("entities") or [], ensure_ascii=False),
                    "index_text": f"{safe_case['question']} {text}" if is_seed else text,
                    "status": str(raw.get("status") or "active"),
                    "confidence": 1.0,
                    "importance": 0.5,
                    "valid_from": "2026-01-01T00:00:00+00:00",
                    "recorded_from": "2026-01-01T00:00:00+00:00",
                }
                if embedder is not None and is_seed:
                    claim.update(
                        {
                            "embedding_dense": embedder.embed_one(text),
                            "embedding_model": "fixture",
                            "embedding_dim": settings.embedding_dim,
                        }
                    )
                repo.insert_claim(claim)
                for event_id in raw.get("evidence_event_ids") or []:
                    connection.execute(
                        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            f"ev:{raw['claim_id']}:{event_id}",
                            "claim",
                            str(raw["claim_id"]),
                            "event",
                            str(event_id),
                            "supports",
                            1.0,
                        ),
                    )
            for index, relation in enumerate(safe_case.get("relations") or [], start=1):
                connection.execute(
                    "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,evidence_json,created_at) "
                    "VALUES (?,?,?,?,?,'[]',?)",
                    (
                        f"relation:{index}",
                        str(relation["from_id"]),
                        str(relation["to_id"]),
                        str(relation["relation"]),
                        float(relation.get("confidence", 1.0)),
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            connection.commit()
    finally:
        database.close()


def materialize_visible_case_cached(
    db_path: Path,
    case: Mapping[str, Any],
    settings: Settings,
    *,
    embedder: EmbedderProtocol | None = None,
) -> Path:
    """物化可重放 dev cache；相同输入复用冻结字节，避免 migration 时间戳漂移。"""
    safe_case = {key: value for key, value in case.items() if key not in _LEAK_KEYS}
    assert_gold_free(safe_case)
    source_root = Path(__file__).resolve().parents[1]
    migrations = source_root / "storage" / "migrations"
    fingerprint_payload = {
        "case": safe_case,
        "settings": {
            "embedding_dim": settings.embedding_dim,
            "vector_backend": str(settings.vector_backend),
            "packed_context_token_budget": settings.packed_context_token_budget,
        },
        "materializer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "database_sha256": hashlib.sha256((source_root / "storage" / "database.py").read_bytes()).hexdigest(),
        "migrations": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(migrations.glob("*.sql"))
        },
        "embedder": type(embedder).__name__ if embedder is not None else None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path = db_path.with_suffix(".manifest.json")
    if db_path.is_file() and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_db_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
        if manifest.get("fingerprint") == fingerprint and manifest.get("db_sha256") == actual_db_sha:
            return manifest_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = db_path.parent.resolve()
    for stale in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), manifest_path):
        if stale.resolve().parent != resolved_parent:
            raise RuntimeError("dev cache cleanup path escaped its root")
        stale.unlink(missing_ok=True)
    materialize_visible_case(db_path, safe_case, settings, embedder=embedder)
    payload = {
        "schema_version": 1,
        "case_id": safe_case["case_id"],
        "fingerprint": fingerprint,
        "db_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "contains_gold": False,
    }
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return manifest_path
