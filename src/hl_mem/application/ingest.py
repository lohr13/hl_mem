"""记忆写入应用服务。处理事件接收、记忆保存、Claim 提取管线、去重和冲突检测。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hl_mem.observability.audit import current_audit
from hl_mem.protocols import EmbedderProtocol
from hl_mem.recall.attribute_map import validate_canonical_attribute
from hl_mem.recall.conflict import (
    ConflictResolver,
    compute_claim_pair_key,
    compute_conflict_key,
    compute_legacy_conflict_key,
)
from hl_mem.recall.dedup import Deduplicator
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2
from hl_mem.storage.repository import ClaimRepository, EvidenceRepository, EventRepository, JobRepository


def new_id() -> str:
    """生成无分隔符的随机标识。"""
    return uuid.uuid4().hex


def claim_text(claim: dict[str, Any]) -> str:
    """生成用于向量化的 claim 文本。"""
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"


def compute_fact_hash(subject: str, predicate: str, value: Any) -> str:
    """按当前版本规则计算事实哈希。"""
    return compute_fact_hash_v2(subject, predicate, value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(claim: Any) -> dict[str, Any]:
    value = claim.get("value_json", getattr(claim, "value", None))
    return {
        "subject": claim.get("subject_entity_id", getattr(claim, "subject", None)),
        "predicate": claim.get("predicate", getattr(claim, "predicate", None)),
        "value_hash": hashlib.sha256(str(value).encode()).hexdigest(),
        "confidence": claim.get("confidence", getattr(claim, "confidence", None)),
        "status": claim.get("status"),
    }


class IngestService:
    """记忆写入应用服务，拥有事件和任务写入的事务边界。"""

    def __init__(self, connection: Any, embedder: Any) -> None:
        self.connection = connection
        self.embedder = embedder

    def ingest_event(
        self,
        event: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """写入事件并创建提取任务，返回事件标识及是否新建。"""
        key = idempotency_key or event.get("idempotency_key")
        existing = (
            self.connection.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone()
            if key
            else None
        )
        if existing:
            return {"id": existing["id"], "created": False}

        event_id = event.get("id") or new_id()
        timestamp = _now()
        content = event.get("content", {})
        content = content if isinstance(content, dict) else {"text": content}
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        stored_event = {key: value for key, value in event.items() if key not in {"content", "id"}}
        stored_event.update(
            id=event_id,
            idempotency_key=key,
            content_json=content_json,
            occurred_at=event.get("occurred_at") or timestamp,
            recorded_at=timestamp,
            content_hash=hashlib.sha256(content_json.encode()).hexdigest(),
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            created = EventRepository(self.connection).insert_event(stored_event, commit=False)
            if created:
                self._queue_event(event_id, timestamp, commit=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"id": event_id, "created": created}

    def save_explicit_memory(
        self,
        text: str,
        subject: str = "用户",
        predicate: str = "explicit_memory",
        qualifiers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入显式记忆事件并排队，返回事件标识。"""
        timestamp, event_id = _now(), new_id()
        memory = {
            "text": text,
            "subject": subject,
            "predicate": predicate,
            "qualifiers": qualifiers or {},
        }
        content_json = json.dumps({"text": text, "memory": memory}, ensure_ascii=False)
        event = {
            "id": event_id,
            "idempotency_key": None,
            "tenant_id": "default",
            "event_type": "explicit_memory",
            "actor_type": "user",
            "content_json": content_json,
            "occurred_at": timestamp,
            "recorded_at": timestamp,
        }
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            EventRepository(self.connection).insert_event(event, commit=False)
            self._queue_event(event_id, timestamp, commit=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {"id": event_id}

    def _queue_event(self, event_id: str, now: str, commit: bool = True) -> None:
        JobRepository(self.connection).insert_job(
            {
                "id": new_id(),
                "job_type": "extract_event",
                "payload_json": json.dumps({"event_id": event_id}),
                "idempotency_key": f"extract:{event_id}",
                "created_at": now,
                "updated_at": now,
            },
            commit=commit,
        )

    @staticmethod
    def store_extracted(
        connection: Any,
        extracted: Any,
        event: dict[str, Any],
        now: str,
        embedder: EmbedderProtocol,
        authority: str | None = None,
        ttl_days: int = 7,
    ) -> str:
        """持久化提取出的 claim，并执行精确、冲突及语义去重。"""
        audit = current_audit()
        claims, evidence = ClaimRepository(connection), EvidenceRepository(connection)
        namespace, subject = event.get("tenant_id", "default"), extracted.subject
        qualifiers = extracted.qualifiers or {}
        canonical_attribute = validate_canonical_attribute(
            extracted.predicate, getattr(extracted, "canonical_attribute", None)
        )
        value_json = json.dumps(extracted.value, ensure_ascii=False, sort_keys=True)
        scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
        expires_at = (
            (datetime.fromisoformat(now) + timedelta(days=ttl_days)).isoformat()
            if extracted.volatility == "ephemeral" and scope == "temporal"
            else None
        )
        try:
            importance = min(1.0, max(0.0, float(extracted.importance)))
        except (TypeError, ValueError):
            importance = 0.5
        claim = {
            "id": new_id(),
            "namespace_key": namespace,
            "subject_entity_id": subject,
            "predicate": extracted.predicate,
            "value_json": value_json,
            "canonical_attribute": canonical_attribute,
            "fact_hash": compute_fact_hash(subject, extracted.predicate, extracted.value),
            "qualifiers_json": json.dumps(qualifiers, ensure_ascii=False, sort_keys=True),
            "conflict_key": compute_conflict_key(namespace, subject, canonical_attribute, qualifiers),
            "conflict_key_version": 2,
            "legacy_conflict_key": compute_legacy_conflict_key(namespace, subject, extracted.predicate, qualifiers),
            "valid_from": event.get("occurred_at", now),
            "recorded_from": now,
            "observed_at": event.get("occurred_at", now),
            "expires_at": expires_at,
            "volatility": extracted.volatility,
            "status": "active",
            "confidence": extracted.confidence,
            "scope": scope,
            "importance": importance,
            "access_count": 0,
            "last_accessed_at": None,
            "source_authority": authority or ("low" if event.get("actor_type") == "assistant" else "medium"),
            "extractor_version": "llm-v1" if event.get("extractor") == "llm" else "fake-v1",
            "embedding_model": getattr(embedder, "model", "fake"),
            "embedding_dim": embedder.dim,
        }
        started = time.perf_counter_ns()
        exact = claims.find_by_fact_hash(namespace, claim["fact_hash"])
        audit.emit(
            "dedup", "fact_hash_checked", "match" if exact else "new", event_id=event["id"],
            claim_id=claim["id"], related_claim_id=exact["id"] if exact else None,
            duration_us=(time.perf_counter_ns() - started) // 1000,
            detail={"fact_hash": claim["fact_hash"], "predicate": claim["predicate"]},
        )
        if exact:
            _link_event_atomically(connection, evidence, exact["id"], event["id"])
            return exact["id"]
        existing = claims.find_by_conflict_key(claim["conflict_key"])
        superseded_old_id: str | None = None
        resolution: str | None = None
        current: dict[str, Any] | None = None
        if existing:
            started = time.perf_counter_ns()
            current = existing[0]
            resolution = ConflictResolver().resolve(current, {**claim, "qualifiers": qualifiers})
            audit.emit(
                "conflict", "resolved", resolution, event_id=event["id"], claim_id=claim["id"],
                related_claim_id=current["id"], duration_us=(time.perf_counter_ns() - started) // 1000,
                detail={"conflict_key": claim["conflict_key"], "candidate_count": len(existing),
                        "old": _summary(current), "new": _summary(claim)},
            )
            if resolution == "entails":
                _link_event_atomically(connection, evidence, current["id"], event["id"])
                return current["id"]
            if resolution == "state_change":
                claim["supersedes_id"] = current["id"]
                superseded_old_id = current["id"]
            elif resolution == "contradicts":
                claim["status"] = "disputed"
            elif resolution == "uncertain":
                claim["status"] = "candidate"
        else:
            audit.emit(
                "conflict", "not_applicable", "no_existing", event_id=event["id"], claim_id=claim["id"],
                detail={"conflict_key": claim["conflict_key"]},
            )
            claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
            started = time.perf_counter_ns()
            duplicate_id, _ = Deduplicator(claims, embedder).find_duplicate(claim)
            audit.emit(
                "dedup", "semantic_checked", "match" if duplicate_id else "new", event_id=event["id"],
                claim_id=claim["id"], related_claim_id=duplicate_id,
                duration_us=(time.perf_counter_ns() - started) // 1000,
                detail={"matched": duplicate_id is not None},
            )
            if duplicate_id:
                _link_event_atomically(connection, evidence, duplicate_id, event["id"])
                return duplicate_id
        if "embedding_dense" not in claim:
            claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
        connection.execute("BEGIN IMMEDIATE")
        try:
            if current is not None and resolution == "contradicts":
                claims.update_status(current["id"], "disputed", commit=False)
            claims.insert_claim(claim, commit=False)
            if current is not None and resolution in {"contradicts", "uncertain"}:
                connection.execute(
                    "INSERT OR IGNORE INTO conflict_cases "
                    "(id,pair_key,left_claim_id,right_claim_id,status,decision,confidence,rationale,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        new_id(),
                        compute_claim_pair_key(current["id"], claim["id"]),
                        current["id"],
                        claim["id"],
                        "manual_required",
                        resolution,
                        None,
                        "deterministic_ingest_resolution",
                        now,
                    ),
                )
            if superseded_old_id:
                claims.supersede_with_inline(
                    superseded_old_id, claim["id"], extracted.value, claim["valid_from"], now, commit=False
                )
            _link_event(evidence, claim["id"], event["id"], commit=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return claim["id"]


def _link_event(repo: EvidenceRepository, claim_id: str, event_id: str, commit: bool = True) -> None:
    repo.add_link(
        {"id": new_id(), "derived_type": "claim", "derived_id": claim_id, "evidence_type": "event",
         "evidence_id": event_id, "relation": "derived_from", "weight": 1.0},
        commit=commit,
    )


def _link_event_atomically(connection: Any, repo: EvidenceRepository, claim_id: str, event_id: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        _link_event(repo, claim_id, event_id, commit=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
