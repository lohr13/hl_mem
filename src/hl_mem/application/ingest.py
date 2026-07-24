"""记忆写入应用服务。处理事件接收、记忆保存、Claim 提取管线、去重和冲突检测。"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from hl_mem.config import ATTRIBUTE_TTL_DAYS
from hl_mem.domain.claims.attributes import (
    is_mutually_exclusive_attribute,
    validate_canonical_attribute,
)
from hl_mem.domain.claims.conflicts import (
    ConflictResolver,
    compute_claim_pair_key,
    compute_conflict_key,
    compute_legacy_conflict_key,
)
from hl_mem.domain.claims.dedup import Deduplicator
from hl_mem.domain.entity import normalize_entity_id
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import EmbedderProtocol
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2
from hl_mem.storage.repository import (
    ClaimRepository,
    EventRepository,
    EvidenceRepository,
    JobRepository,
)


@dataclass
class _ClaimDraft:
    """保存 claim 草稿及规范化阶段产生的元数据。"""

    claim: dict[str, Any]
    qualifiers: dict[str, Any]


def new_id() -> str:
    """生成无分隔符的随机标识。"""
    return uuid.uuid4().hex


def claim_text(claim: dict[str, Any]) -> str:
    """生成用于向量化的 claim 文本。"""
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value', '')}"


def compute_fact_hash(subject: str, predicate: str, value: Any) -> str:
    """按当前版本规则计算事实哈希。"""
    return compute_fact_hash_v2(subject, predicate, value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(claim: Any) -> dict[str, Any]:
    value = claim.get("value", getattr(claim, "value", None))
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
        event_id = event.get("id") or new_id()
        timestamp = _now()
        content = event.get("content", {})
        content = content if isinstance(content, dict) else {"text": content}
        stored_event = {key: value for key, value in event.items() if key not in {"content", "id"}}
        stored_event.update(
            id=event_id,
            idempotency_key=key,
            content=content,
            occurred_at=event.get("occurred_at") or timestamp,
            recorded_at=timestamp,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if key:
                existing_id = EventRepository(self.connection).find_id_by_idempotency_key(key)
                if existing_id:
                    self.connection.commit()
                    return {"id": existing_id, "created": False}
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
        event = {
            "id": event_id,
            "idempotency_key": None,
            "tenant_id": "default",
            "event_type": "explicit_memory",
            "actor_type": "user",
            "content": {"text": text, "memory": memory},
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

    def _queue_event(self, event_id: str, now: str, commit: bool = False) -> None:
        JobRepository(self.connection).insert_job(
            {
                "id": new_id(),
                "job_type": "extract_event",
                "payload": {"event_id": event_id},
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
        draft = _build_claim_drafts(extracted, event, now, embedder, authority, ttl_days)
        claim, qualifiers = draft.claim, draft.qualifiers
        namespace = claim["namespace_key"]
        canonical_attribute = claim["canonical_attribute"]
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def emit_audit_events() -> None:
            for args, kwargs in audit_events:
                audit.emit(*args, **kwargs)

        superseded_old_id: str | None = None
        resolution: str | None = None
        current: dict[str, Any] | None = None
        result_id = claim["id"]
        connection.execute("BEGIN IMMEDIATE")
        try:
            started = time.perf_counter_ns()
            exact, existing = _find_resolution(claims, claim, canonical_attribute)
            audit_events.append(
                (
                    ("dedup", "fact_hash_checked", "match" if exact else "new"),
                    {
                        "event_id": event["id"],
                        "claim_id": claim["id"],
                        "related_claim_id": exact["id"] if exact else None,
                        "duration_us": (time.perf_counter_ns() - started) // 1000,
                        "detail": {
                            "fact_hash": claim["fact_hash"],
                            "predicate": claim["predicate"],
                        },
                    },
                )
            )
            if exact:
                _link_event(evidence, exact["id"], event["id"], commit=False)
                result_id = exact["id"]
                connection.commit()
                emit_audit_events()
                return result_id

            if existing:
                started = time.perf_counter_ns()
                current = existing[0]
                resolution = ConflictResolver().resolve(current, {**claim, "qualifiers": qualifiers})
                audit_events.append(
                    (
                        ("conflict", "resolved", resolution),
                        {
                            "event_id": event["id"],
                            "claim_id": claim["id"],
                            "related_claim_id": current["id"],
                            "duration_us": (time.perf_counter_ns() - started) // 1000,
                            "detail": {
                                "conflict_key": claim["conflict_key"],
                                "candidate_count": len(existing),
                                "old": _summary(current),
                                "new": _summary(claim),
                            },
                        },
                    )
                )
                if resolution == "entails":
                    _link_event(evidence, current["id"], event["id"], commit=False)
                    result_id = current["id"]
                    connection.commit()
                    emit_audit_events()
                    return result_id
                if resolution == "state_change":
                    claim["supersedes_id"] = current["id"]
                    superseded_old_id = current["id"]
                elif resolution == "contradicts":
                    claim["status"] = "disputed"
                elif resolution == "uncertain":
                    claim["status"] = "candidate"
            else:
                audit_events.append(
                    (
                        ("conflict", "not_applicable", "no_existing"),
                        {
                            "event_id": event["id"],
                            "claim_id": claim["id"],
                            "detail": {"conflict_key": claim["conflict_key"]},
                        },
                    )
                )
                started = time.perf_counter_ns()
                duplicate_id, _ = Deduplicator(claims, embedder).find_duplicate(claim)
                audit_events.append(
                    (
                        (
                            "dedup",
                            "semantic_checked",
                            "match" if duplicate_id else "new",
                        ),
                        {
                            "event_id": event["id"],
                            "claim_id": claim["id"],
                            "related_claim_id": duplicate_id,
                            "duration_us": (time.perf_counter_ns() - started) // 1000,
                            "detail": {"matched": duplicate_id is not None},
                        },
                    )
                )
                if duplicate_id:
                    _link_event(evidence, duplicate_id, event["id"], commit=False)
                    result_id = duplicate_id
                    connection.commit()
                    emit_audit_events()
                    return result_id

            inserted = _persist_resolution(claims, claim)
            if not inserted:
                winner = claims.find_by_fact_hash(namespace, claim["fact_hash"])
                if winner:
                    _link_event(evidence, winner["id"], event["id"], commit=False)
                    result_id = winner["id"]
                connection.commit()
                emit_audit_events()
                return result_id

            if current is not None and resolution == "contradicts":
                claims.update_status(current["id"], "disputed", commit=False)
            if current is not None and resolution in {"contradicts", "uncertain"}:
                claims.insert_conflict_case(
                    {
                        "id": new_id(),
                        "pair_key": compute_claim_pair_key(current["id"], claim["id"]),
                        "left_claim_id": current["id"],
                        "right_claim_id": claim["id"],
                        "status": "manual_required",
                        "decision": resolution,
                        "confidence": None,
                        "rationale": "deterministic_ingest_resolution",
                        "created_at": now,
                    },
                    commit=False,
                )
            if superseded_old_id:
                claims.supersede_with_inline(
                    superseded_old_id,
                    claim["id"],
                    extracted.value,
                    claim["valid_from"],
                    now,
                    commit=False,
                )
            _link_event(evidence, claim["id"], event["id"], commit=False)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        emit_audit_events()
        return result_id


def _build_claim_drafts(
    extracted: Any,
    event: dict[str, Any],
    now: str,
    embedder: EmbedderProtocol,
    authority: str | None,
    ttl_days: int,
) -> _ClaimDraft:
    """阶段 1：规范化提取结果、计算 TTL 并生成 claim 草稿。"""
    # NOTE: tenant_id/namespace 当前是单租户部署中的软标签，不是隔离边界。
    # 多租户需要未来引入统一 NamespaceContext 并贯穿后台任务与存储访问。
    namespace = event.get("tenant_id", "default")
    subject = normalize_entity_id(extracted.subject)
    qualifiers = extracted.qualifiers or {}
    canonical_attribute = validate_canonical_attribute(
        extracted.predicate, getattr(extracted, "canonical_attribute", None)
    )
    scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
    attribute_ttl_days = ATTRIBUTE_TTL_DAYS.get(canonical_attribute)
    effective_ttl_days = (
        attribute_ttl_days
        if attribute_ttl_days is not None
        else ttl_days if extracted.volatility == "ephemeral" and scope == "temporal" else None
    )
    expires_at = (
        (datetime.fromisoformat(now) + timedelta(days=effective_ttl_days)).isoformat()
        if effective_ttl_days is not None
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
        "value": extracted.value,
        "canonical_attribute": canonical_attribute,
        "fact_hash": compute_fact_hash(subject, extracted.predicate, extracted.value),
        "qualifiers": qualifiers,
        "conflict_key": compute_conflict_key(namespace, subject, canonical_attribute, qualifiers),
        "conflict_key_version": 2,
        "legacy_conflict_key": compute_legacy_conflict_key(namespace, subject, extracted.predicate, qualifiers),
        "valid_from": event.get("occurred_at", now),
        "recorded_from": now,
        "observed_at": event.get("occurred_at", now),
        "expires_at": expires_at,
        "volatility": "ephemeral" if attribute_ttl_days is not None else extracted.volatility,
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
    claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
    return _ClaimDraft(claim, qualifiers)


def _find_resolution(
    claims: ClaimRepository,
    claim: dict[str, Any],
    canonical_attribute: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """阶段 2：查找精确重复项和互斥属性的待解析候选。"""
    exact = claims.find_by_fact_hash(claim["namespace_key"], claim["fact_hash"])
    exclusive = is_mutually_exclusive_attribute(canonical_attribute)
    existing = claims.find_by_conflict_key(claim["conflict_key"]) if exclusive and exact is None else []
    return exact, existing


def _persist_resolution(claims: ClaimRepository, claim: dict[str, Any]) -> bool:
    """阶段 3：在调用方已开启的事务中写入解析后的 claim。"""
    return claims.insert_claim(claim, commit=False)


def _link_event(repo: EvidenceRepository, claim_id: str, event_id: str, commit: bool = False) -> None:
    repo.add_link(
        {
            "id": new_id(),
            "derived_type": "claim",
            "derived_id": claim_id,
            "evidence_type": "event",
            "evidence_id": event_id,
            "relation": "derived_from",
            "weight": 1.0,
        },
        commit=commit,
    )


def _link_event_atomically(connection: Any, repo: EvidenceRepository, claim_id: str, event_id: str) -> None:
    """已弃用：在独立事务中关联事件证据。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        _link_event(repo, claim_id, event_id, commit=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
