"""记忆写入应用服务。处理事件接收、记忆保存、Claim 提取管线、去重和冲突检测。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from hl_mem.application._ingest_resolution import (
    _converge_entailed_group,
    _find_resolution,
    _insert_pending_dedup_pair_row,
    _persist_resolution,
    _quarantine_conflict_group,
    _quarantine_temporal_pair,
    _resolve_conflict_group,
    _resolve_temporal_candidates,
)
from hl_mem.application.ingest_coordinates import IngestCoordinateProjection
from hl_mem.application.ingest_evidence import link_source_events as _link_source_events
from hl_mem.application.latest_wins import begin_latest_wins, finish_latest_wins
from hl_mem.application.provenance_admission import (
    GovernedClaimInput,
    govern_claim_input,
    provenance_audit_event,
)
from hl_mem.config import INGEST_DEDUP_PAIR_SIMILARITY_FLOOR
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.attributes import (
    is_mutually_exclusive_attribute,
    normalize_topic_tags,
    predicate_for_canonical_attribute,
    validate_canonical_attribute,
    validate_slot_instance,
)
from hl_mem.domain.claims.claim import IndexTextMode, build_index_text
from hl_mem.domain.claims.conflicts import (
    compute_conflict_key,
    compute_legacy_conflict_key,
)
from hl_mem.domain.claims.dedup import Deduplicator
from hl_mem.domain.claims.retention import (
    TTLPolicy,
    compute_expiration,
    normalize_utc_iso,
)
from hl_mem.domain.constants import DEFAULT_SUBJECT
from hl_mem.domain.entity import (
    invalid_subject_reason,
    isolated_subject_id,
    normalize_entity_id,
)
from hl_mem.domain.provenance import validate_event_provenance
from hl_mem.errors import ConflictError, ValidationError
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.lifecycle import assert_transition
from hl_mem.monitoring.metrics import DEFAULT_ADMISSION_METRICS, AdmissionMetrics
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import EmbedderProtocol, ExtractorProtocol
from hl_mem.settings import Settings
from hl_mem.state_latest_wins import CurrentnessProof
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2


@dataclass
class _ClaimDraft:
    """保存 claim 草稿及规范化阶段产生的元数据。"""

    claim: dict[str, Any]
    qualifiers: dict[str, Any]


@dataclass(frozen=True)
class _ClaimDraftContext:
    policy: TTLPolicy | None
    ttl_days: int | None
    now: str
    embedder: EmbedderProtocol
    index_text_mode: IndexTextMode
    trusted_projector_slot: str | None


def _prepare_governed_claim(
    connection: sqlite3.Connection,
    extracted: ExtractedClaim,
    event: dict[str, Any],
    source_events: Sequence[dict[str, Any]] | None,
    authority: str | None,
    context: _ClaimDraftContext,
) -> tuple[list[dict[str, Any]], GovernedClaimInput, _ClaimDraft | StoreClaimResult]:
    evidence_events = IngestService._validate_source_events(event, source_events)
    governed = govern_claim_input(connection, extracted, authority, evidence_events)
    if not governed.admission.allowed:
        return evidence_events, governed, StoreClaimResult(None, "skipped", governed.admission.reason_code)
    draft = _build_claim_drafts(
        governed.extracted,
        event,
        context.now,
        context.embedder,
        governed.authority,
        IngestService._retention_policy(context.policy, context.ttl_days),
        context.index_text_mode,
        context.trusted_projector_slot,
    )
    return evidence_events, governed, draft


@dataclass(frozen=True)
class StoreClaimResult:
    """记录 claim 写入结果及写入或拒绝原因。"""

    claim_id: str | None
    status: str
    reason: str


@dataclass(frozen=True)
class _IngestResolution:
    """Named outputs from one pre-persistence resolution branch."""

    conflict_entails: StoreClaimResult | None = None
    temporal_entails: StoreClaimResult | None = None
    semantic_duplicate: StoreClaimResult | None = None
    superseded_member_ids: tuple[str, ...] = ()
    review_group: tuple[dict[str, Any], ...] = ()
    review_decision: str | None = None
    review_rationale: str | None = None
    temporal_review_candidates: tuple[dict[str, Any], ...] = ()
    temporal_backfill_tip: dict[str, Any] | None = None
    semantic_candidate_id: str | None = None
    semantic_candidate_similarity: float | None = None


def _flush_audit_events(audit: Any, events: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> None:
    for args, kwargs in events:
        audit.emit(*args, **kwargs)


def _commit_store_result(
    connection: sqlite3.Connection,
    audit: Any,
    audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]],
    result: StoreClaimResult,
) -> StoreClaimResult:
    connection.commit()
    _flush_audit_events(audit, audit_events)
    return result


def _retention_anchor(
    observed_at: str,
    recorded_from: str,
    *,
    memory_layer: str,
    is_plan: bool,
    occurred_start: str | None,
    occurred_end: str | None,
) -> str:
    """Select one normalized TTL anchor without changing the Claim's event time."""
    if not is_plan:
        return recorded_from if memory_layer == "episodic" else observed_at
    anchors = [recorded_from]
    for field_name, value in (("occurred_start", occurred_start), ("occurred_end", occurred_end)):
        if value:
            anchors.append(normalize_utc_iso(value, field_name))
    return max(anchors, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))


def new_id() -> str:
    """生成无分隔符的随机标识。"""
    return uuid.uuid4().hex


def claim_text(claim: dict[str, Any]) -> str:
    """生成用于向量化的 claim 文本。"""
    return str(claim.get("index_text") or build_index_text(claim))


def compute_fact_hash(subject: str, predicate: str, value: Any) -> str:
    """按当前版本规则计算事实哈希。"""
    return compute_fact_hash_v2(subject, predicate, value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(claim: ExtractedClaim | dict[str, Any]) -> dict[str, Any]:
    if isinstance(claim, ExtractedClaim):
        return {
            "subject": claim.subject,
            "predicate": claim.predicate,
            "value_hash": hashlib.sha256(str(claim.value).encode()).hexdigest(),
            "confidence": claim.confidence,
            "status": None,
        }
    value = claim.get("value")
    return {
        "subject": claim.get("subject_entity_id", getattr(claim, "subject", None)),
        "predicate": claim.get("predicate", getattr(claim, "predicate", None)),
        "value_hash": hashlib.sha256(str(value).encode()).hexdigest(),
        "confidence": claim.get("confidence", getattr(claim, "confidence", None)),
        "status": claim.get("status"),
    }


def _event_namespace(event: dict[str, Any]) -> str:
    """解析 namespace/tenant_id 兼容字段，并拒绝含糊的双重指定。"""
    namespace = event.get("namespace")
    tenant_id = event.get("tenant_id")
    if namespace is not None and tenant_id is not None and namespace != tenant_id:
        raise ValidationError("namespace and tenant_id must match when both are provided")
    resolved = namespace if namespace is not None else tenant_id
    resolved = "default" if resolved is None else resolved
    if not isinstance(resolved, str) or not resolved:
        raise ValidationError("namespace must be a non-empty string")
    return resolved


def _canonical_event_payload(
    event: dict[str, Any],
    *,
    include_id: bool = False,
    include_occurred_at: bool = False,
) -> str:
    """生成用于幂等冲突判断的稳定事件载荷。"""
    content = event.get("content", {})
    content = content if isinstance(content, dict) else {"text": content}
    payload = {
        "tenant_id": _event_namespace(event),
        "user_id": event.get("user_id"),
        "project_id": event.get("project_id"),
        "agent_id": event.get("agent_id"),
        "session_id": event.get("session_id"),
        "event_type": event.get("event_type", "message"),
        "actor_type": event.get("actor_type", "user"),
        "actor_id": event.get("actor_id"),
        "content": content,
        "metadata": event.get("metadata") or {},
        "source_uri": event.get("source_uri"),
        "sensitivity": event.get("sensitivity", "normal"),
        "origin_class": event.get("origin_class", "unknown"),
        "session_kind": event.get("session_kind", "unknown"),
    }
    if include_id:
        payload["id"] = event.get("id")
    if include_occurred_at:
        payload["occurred_at"] = event.get("occurred_at")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class IngestService:
    """记忆写入应用服务，拥有事件和任务写入的事务边界。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def dry_run_extract(
        extractor: ExtractorProtocol,
        text: str,
        context: dict[str, Any] | None = None,
        custom_instructions: str | None = None,
    ) -> dict[str, Any]:
        """仅执行 claim 提取并返回结果与 token 用量，不写入任何记忆数据。"""
        extraction_context = dict(context or {})
        if custom_instructions is not None:
            extraction_context["custom_instructions"] = custom_instructions
        claims = extractor.extract({"text": text}, extraction_context)
        serialized_claims = []
        for claim in claims:
            serialized = asdict(claim) if is_dataclass(claim) else dict(claim)
            serialized.pop("source_event_indices", None)
            serialized_claims.append(serialized)
        return {
            "claims": serialized_claims,
            "usage": {
                "total_tokens": int(getattr(extractor, "last_usage_tokens", 0)),
                "input_tokens": int(getattr(extractor, "last_input_tokens", 0)),
                "output_tokens": int(getattr(extractor, "last_output_tokens", 0)),
            },
        }

    def ingest_event(
        self,
        event: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """写入事件并创建提取任务，返回事件标识及是否新建。"""
        queued_event = dict(event)
        if idempotency_key is not None:
            queued_event["idempotency_key"] = idempotency_key
        return self.ingest_events([queued_event])[0]

    def ingest_events(self, events_to_ingest: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """在一个事务中写入一组 Event，并继续为每个 Event 保留独立提取 job。"""
        if not events_to_ingest:
            raise ValidationError("events must not be empty")
        timestamp = _now()
        timestamp_value = datetime.fromisoformat(timestamp)
        prepared: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
        for index, event in enumerate(events_to_ingest):
            provenance = validate_event_provenance(event)
            key = event.get("idempotency_key")
            event_id = event.get("id") or new_id()
            content = event.get("content", {})
            content = content if isinstance(content, dict) else {"text": content}
            namespace = _event_namespace(event)
            stored_event = {
                field: value
                for field, value in event.items()
                if field not in {"content", "id", "namespace"} and not (field == "metadata" and value is None)
            }
            recorded_at = (timestamp_value + timedelta(microseconds=index)).isoformat()
            stored_event.update(
                id=event_id,
                idempotency_key=key,
                tenant_id=namespace,
                content=content,
                occurred_at=event.get("occurred_at") or timestamp,
                recorded_at=recorded_at,
                origin_class=provenance.origin_class,
                session_kind=provenance.session_kind,
            )
            prepared.append((event, stored_event, key))
        results: list[dict[str, Any]] = []
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            repository = EventRepository(self.connection)
            for original, stored_event, key in prepared:
                event_id = str(stored_event["id"])
                if key:
                    existing_id = repository.find_id_by_idempotency_key(str(key))
                    if existing_id:
                        existing = repository.get_event(existing_id)
                        if existing is None:
                            raise RuntimeError(f"idempotent event disappeared: {existing_id}")
                        include_id = original.get("id") is not None
                        include_occurred_at = original.get("occurred_at") is not None
                        if _canonical_event_payload(
                            existing,
                            include_id=include_id,
                            include_occurred_at=include_occurred_at,
                        ) != _canonical_event_payload(
                            stored_event,
                            include_id=include_id,
                            include_occurred_at=include_occurred_at,
                        ):
                            raise ConflictError(
                                f"idempotency key {key!r} was already used with a different event payload"
                            )
                        results.append({"id": existing_id, "created": False})
                        continue
                created = repository.insert_event(stored_event, commit=False)
                if created:
                    self._queue_event(event_id, str(stored_event["recorded_at"]), commit=False)
                results.append({"id": event_id, "created": created})
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return results

    def save_explicit_memory(
        self,
        text: str,
        subject: str = DEFAULT_SUBJECT,
        predicate: str = "explicit_memory",
        qualifiers: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        namespace: str = "default",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """经统一事件事务写入显式记忆，并返回真实的新建状态。"""
        memory = {
            "text": text,
            "subject": subject,
            "predicate": predicate,
            "qualifiers": qualifiers or {},
        }
        event = {
            "tenant_id": namespace,
            "event_type": "explicit_memory",
            "actor_type": "user",
            "content": {"text": text, "memory": memory},
        }
        if session_id:
            event["session_id"] = session_id
        return self.ingest_event(
            event,
            idempotency_key=idempotency_key,
        )

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
        connection: sqlite3.Connection,
        extracted: ExtractedClaim,
        event: dict[str, Any],
        now: str,
        embedder: EmbedderProtocol,
        authority: str | None = None,
        ttl_days: int | None = None,
        policy: TTLPolicy | None = None,
        relation_discovery_mode: str = "off",
        index_text_mode: IndexTextMode = "natural",
        source_events: Sequence[dict[str, Any]] | None = None,
        price_target_mode: str | None = None,
        currentness_proof: CurrentnessProof | None = None,
        _trusted_projector_slot: str | None = None,
    ) -> StoreClaimResult:
        """持久化提取出的 claim，并执行精确、冲突及语义去重。"""
        audit = current_audit()
        claims, evidence = ClaimRepository(connection), EvidenceRepository(connection)
        evidence_events, governed, draft = _prepare_governed_claim(
            connection,
            extracted,
            event,
            source_events,
            authority,
            _ClaimDraftContext(policy, ttl_days, now, embedder, index_text_mode, _trusted_projector_slot),
        )
        if isinstance(draft, StoreClaimResult):
            if not governed.admission.preserve_existing:
                provenance_event = provenance_audit_event(event, governed)
                audit.emit(*provenance_event[0], **provenance_event[1])
            IngestService._emit_rejected_claim(audit, draft, event, governed.extracted)
            return draft
        claim, qualifiers = draft.claim, draft.qualifiers
        namespace = claim["namespace_key"]
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        if not governed.admission.preserve_existing:
            audit_events.append(provenance_audit_event(event, governed))
        result_id = claim["id"]
        connection.execute("BEGIN IMMEDIATE")
        try:
            projection = IngestCoordinateProjection.prepare(connection, claim, now, price_target_mode, str(event["id"]))
            audit_events.append(projection.audit_event)
            latest_wins, early_latest_wins = begin_latest_wins(
                connection, claim, evidence_events, currentness_proof, audit_events
            )
            if early_latest_wins:
                return _commit_store_result(
                    connection,
                    audit,
                    audit_events,
                    StoreClaimResult(early_latest_wins[0], "stored", early_latest_wins[1]),
                )
            started = time.perf_counter_ns()
            exact, existing = _find_resolution(claims, claim)
            IngestService._record_fact_hash_check(
                audit_events,
                event["id"],
                claim,
                exact,
                started,
            )
            if latest_wins is not None:
                exact, existing = None, []
            if exact:
                exact_result = IngestService._resolve_exact_duplicate(
                    claims,
                    evidence,
                    exact,
                    claim,
                    qualifiers,
                    evidence_events,
                    now,
                    event["id"],
                    audit_events,
                )
                return _commit_store_result(connection, audit, audit_events, exact_result)
            resolution: _IngestResolution | None
            if existing:
                resolution = IngestService._resolve_conflict_candidates(
                    claims,
                    evidence,
                    existing,
                    claim,
                    qualifiers,
                    evidence_events,
                    namespace,
                    now,
                    event["id"],
                    audit_events,
                )
                if resolution.conflict_entails is not None:
                    return _commit_store_result(connection, audit, audit_events, resolution.conflict_entails)
            else:
                resolution = IngestService._resolve_temporal_candidate_branch(
                    claims,
                    evidence,
                    claim,
                    evidence_events,
                    event["id"],
                    audit_events,
                )
                if resolution is None:
                    resolution = IngestService._resolve_semantic_candidate_branch(
                        claims,
                        evidence,
                        claim,
                        evidence_events,
                        embedder,
                        event["id"],
                        audit_events,
                    )
                if resolution.temporal_entails is not None:
                    return _commit_store_result(connection, audit, audit_events, resolution.temporal_entails)
                if resolution.semantic_duplicate is not None:
                    return _commit_store_result(connection, audit, audit_events, resolution.semantic_duplicate)

            inserted = _persist_resolution(claims, claim)
            if not inserted:
                winner = claims.find_by_fact_hash(namespace, claim["fact_hash"])
                if winner:
                    _link_source_events(evidence, winner["id"], evidence_events)
                    result_id = winner["id"]
                return _commit_store_result(
                    connection, audit, audit_events, StoreClaimResult(result_id, "stored", "concurrent_duplicate")
                )

            if (
                resolution.semantic_candidate_id is not None
                and resolution.semantic_candidate_similarity is not None
                and resolution.semantic_candidate_similarity >= INGEST_DEDUP_PAIR_SIMILARITY_FLOOR
            ):
                _insert_pending_dedup_pair(
                    connection,
                    resolution.semantic_candidate_id,
                    claim,
                    resolution.semantic_candidate_similarity,
                    now,
                )

            IngestService._quarantine_resolution(claims, claim, now, resolution)
            IngestService._converge_superseded_members(claims, claim, now, resolution)
            _link_source_events(evidence, claim["id"], evidence_events)
            finish_latest_wins(latest_wins, claims, claim, now, audit_events)
            projection.persist_and_queue(result_id, now)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        _flush_audit_events(audit, audit_events)
        if relation_discovery_mode != "off":
            JobRepository(connection).insert_job(
                {
                    "id": new_id(),
                    "job_type": "discover_relations",
                    "payload": {"claim_id": result_id},
                    "idempotency_key": f"relation-discovery:{result_id}",
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return StoreClaimResult(result_id, "stored", "inserted")

    @staticmethod
    def _validate_source_events(
        event: dict[str, Any],
        source_events: Sequence[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        evidence_events = list(source_events or [event])
        if not evidence_events:
            raise ValidationError("source_events must not be empty")
        primary_namespace = _event_namespace(event)
        if any(_event_namespace(source) != primary_namespace for source in evidence_events):
            raise ValidationError("all source events for a claim must share one namespace")
        return evidence_events

    @staticmethod
    def _record_fact_hash_check(
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]],
        event_id: str,
        claim: dict[str, Any],
        exact: dict[str, Any] | None,
        started: int,
    ) -> None:
        audit_events.append(
            (
                ("dedup", "fact_hash_checked", "match" if exact else "new"),
                {
                    "event_id": event_id,
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

    @staticmethod
    def _emit_rejected_claim(
        audit: Any,
        draft: StoreClaimResult,
        event: dict[str, Any],
        extracted: ExtractedClaim,
    ) -> None:
        audit.emit(
            "ingest",
            "claim_write",
            draft.status,
            event_id=event["id"],
            detail={"reason": draft.reason, "importance": getattr(extracted, "importance", None)},
        )

    @staticmethod
    def _retention_policy(policy: TTLPolicy | None, ttl_days: int | None) -> TTLPolicy:
        effective_policy = policy or Settings().retention_policy()
        if ttl_days is None:
            return effective_policy
        return TTLPolicy(
            temporal_ttl_days_low=effective_policy.temporal_ttl_days_low,
            temporal_ttl_days_normal=ttl_days,
            temporal_ttl_days_high=effective_policy.temporal_ttl_days_high,
            importance_low_threshold=effective_policy.importance_low_threshold,
            importance_high_threshold=effective_policy.importance_high_threshold,
            importance_write_floor=effective_policy.importance_write_floor,
            slot_short_ttl_seconds=effective_policy.slot_short_ttl_seconds,
            short_ttl_slots=effective_policy.short_ttl_slots,
        )

    @staticmethod
    def _resolve_exact_duplicate(
        claims: ClaimRepository,
        evidence: EvidenceRepository,
        exact: dict[str, Any],
        claim: dict[str, Any],
        qualifiers: dict[str, Any],
        evidence_events: Sequence[dict[str, Any]],
        now: str,
        event_id: str,
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> StoreClaimResult:
        exact_group = (
            claims.find_by_conflict_key(exact.get("conflict_key"))
            if is_mutually_exclusive_attribute(exact.get("canonical_slot"))
            else []
        )
        group_resolution = (
            _resolve_conflict_group(
                exact_group,
                {**claim, "qualifiers": qualifiers},
                preferred_id=exact["id"],
            )
            if exact_group
            else None
        )
        if group_resolution is not None and group_resolution.outcome != "entails":
            _link_source_events(evidence, exact["id"], evidence_events)
            _quarantine_conflict_group(
                claims,
                exact_group,
                now,
                decision="uncertain",
                rationale=(
                    "ingest_dirty_active_group" if group_resolution.active_count > 1 else "ingest_group_resolution"
                ),
            )
            audit_events.append(
                (
                    ("conflict", "quarantined", "dirty_active_group"),
                    {
                        "event_id": event_id,
                        "claim_id": exact["id"],
                        "detail": {
                            "conflict_key": exact.get("conflict_key"),
                            "member_outcomes": dict(group_resolution.member_outcomes),
                            "group_claim_ids": [item["id"] for item in exact_group],
                        },
                    },
                )
            )
            return StoreClaimResult(exact["id"], "stored", "exact_duplicate_dirty_group")
        result_id = (
            _converge_entailed_group(claims, group_resolution, now) if group_resolution is not None else exact["id"]
        )
        _link_source_events(evidence, result_id, evidence_events)
        return StoreClaimResult(result_id, "stored", "exact_duplicate")

    @staticmethod
    def _resolve_conflict_candidates(
        claims: ClaimRepository,
        evidence: EvidenceRepository,
        existing: Sequence[dict[str, Any]],
        claim: dict[str, Any],
        qualifiers: dict[str, Any],
        evidence_events: Sequence[dict[str, Any]],
        namespace: str,
        now: str,
        event_id: str,
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> _IngestResolution:
        started = time.perf_counter_ns()
        group_resolution = _resolve_conflict_group(existing, {**claim, "qualifiers": qualifiers})
        current = group_resolution.representative
        deterministic_resolution = group_resolution.outcome
        lifecycle = claims.conflict_group_lifecycle(namespace, str(claim["conflict_key"]))
        reopen_after_terminal = lifecycle.latest_terminal_generation is not None and lifecycle.open_case_id is None
        resolution = "uncertain" if reopen_after_terminal else deterministic_resolution
        audit_events.append(
            (
                ("conflict", "resolved", resolution),
                {
                    "event_id": event_id,
                    "claim_id": claim["id"],
                    "related_claim_id": current["id"],
                    "duration_us": (time.perf_counter_ns() - started) // 1000,
                    "detail": {
                        "conflict_key": claim["conflict_key"],
                        "candidate_count": len(existing),
                        "old": _summary(current),
                        "member_outcomes": dict(group_resolution.member_outcomes),
                        "active_count": group_resolution.active_count,
                        "deterministic_resolution": deterministic_resolution,
                        "latest_terminal_generation": lifecycle.latest_terminal_generation,
                        "reopen_after_terminal": reopen_after_terminal,
                        "new": _summary(claim),
                    },
                },
            )
        )
        if resolution == "entails" and not reopen_after_terminal:
            result_id = _converge_entailed_group(claims, group_resolution, now)
            _link_source_events(evidence, result_id, evidence_events)
            return _IngestResolution(conflict_entails=StoreClaimResult(result_id, "stored", "entailed"))
        if reopen_after_terminal:
            claim["status"] = "disputed"
            return _IngestResolution(
                review_group=tuple(existing),
                review_decision="uncertain",
                review_rationale="terminal_generation_reopen",
            )
        if resolution == "state_change":
            claim["status"] = "candidate"
            claim["supersedes_id"] = current["id"]
            return _IngestResolution(
                superseded_member_ids=tuple(item["id"] for item in existing),
            )
        claim["status"] = "disputed"
        return _IngestResolution(
            review_group=tuple(existing),
            review_decision=resolution if resolution in {"contradicts", "uncertain"} else "uncertain",
            review_rationale=(
                "ingest_dirty_active_group" if group_resolution.active_count > 1 else "deterministic_ingest_resolution"
            ),
        )

    @staticmethod
    def _resolve_temporal_candidate_branch(
        claims: ClaimRepository,
        evidence: EvidenceRepository,
        claim: dict[str, Any],
        evidence_events: Sequence[dict[str, Any]],
        event_id: str,
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> _IngestResolution | None:
        temporal_resolution = _resolve_temporal_candidates(claims.find_temporal_candidates(claim), claim)
        if temporal_resolution is not None:
            audit_events.append(
                (
                    ("conflict", "temporal_link", temporal_resolution.outcome),
                    {
                        "event_id": event_id,
                        "claim_id": claim["id"],
                        "related_claim_id": temporal_resolution.representative["id"],
                        "detail": {
                            "member_outcomes": temporal_resolution.member_outcomes,
                            "rule_version": "temporal-v1",
                        },
                    },
                )
            )
            if temporal_resolution.outcome == "entails":
                result_id = str(temporal_resolution.representative["id"])
                _link_source_events(evidence, result_id, evidence_events)
                return _IngestResolution(temporal_entails=StoreClaimResult(result_id, "stored", "temporal_entails"))
            if temporal_resolution.outcome in {"state_change", "snapshot_advance"}:
                claim["status"] = "candidate"
                if temporal_resolution.snapshot_order == "older":
                    return _IngestResolution(temporal_backfill_tip=temporal_resolution.representative)
                claim["supersedes_id"] = temporal_resolution.representative["id"]
                return _IngestResolution(
                    superseded_member_ids=tuple(str(member["id"]) for member in temporal_resolution.members)
                )
            if temporal_resolution.outcome == "distinct_series":
                return _IngestResolution()
            claim["status"] = "disputed"
            return _IngestResolution(
                temporal_review_candidates=temporal_resolution.members,
                review_rationale=f"temporal_update_uncertain:{temporal_resolution.rationale}",
            )
        audit_events.append(
            (
                ("conflict", "not_applicable", "no_existing"),
                {
                    "event_id": event_id,
                    "claim_id": claim["id"],
                    "detail": {"conflict_key": claim["conflict_key"]},
                },
            )
        )
        return None

    @staticmethod
    def _resolve_semantic_candidate_branch(
        claims: ClaimRepository,
        evidence: EvidenceRepository,
        claim: dict[str, Any],
        evidence_events: Sequence[dict[str, Any]],
        embedder: EmbedderProtocol,
        event_id: str,
        audit_events: list[tuple[tuple[Any, ...], dict[str, Any]]],
    ) -> _IngestResolution:
        started = time.perf_counter_ns()
        duplicate_id, dedup_reason = Deduplicator(claims, embedder).find_duplicate(claim)
        is_semantic_candidate = dedup_reason == "semantic_candidate"
        audit_events.append(
            (
                (
                    "dedup",
                    "semantic_checked",
                    "candidate" if is_semantic_candidate else ("match" if duplicate_id else "new"),
                ),
                {
                    "event_id": event_id,
                    "claim_id": claim["id"],
                    "related_claim_id": duplicate_id,
                    "duration_us": (time.perf_counter_ns() - started) // 1000,
                    "detail": {
                        "matched": duplicate_id is not None and not is_semantic_candidate,
                        "candidate": is_semantic_candidate,
                        "reason": dedup_reason,
                    },
                },
            )
        )
        if duplicate_id and not is_semantic_candidate:
            _link_source_events(evidence, duplicate_id, evidence_events)
            return _IngestResolution(semantic_duplicate=StoreClaimResult(duplicate_id, "stored", "semantic_duplicate"))
        if duplicate_id and is_semantic_candidate:
            candidate = claims.get_claim(duplicate_id)
            if candidate and candidate.get("embedding_dense") and claim.get("embedding_dense"):
                return _IngestResolution(
                    semantic_candidate_id=duplicate_id,
                    semantic_candidate_similarity=cosine_similarity(
                        candidate["embedding_dense"], claim["embedding_dense"]
                    ),
                )
        return _IngestResolution()

    @staticmethod
    def _quarantine_resolution(
        claims: ClaimRepository,
        claim: dict[str, Any],
        now: str,
        resolution: _IngestResolution,
    ) -> None:
        if resolution.review_group:
            _quarantine_conflict_group(
                claims,
                [*resolution.review_group, claim],
                now,
                decision=resolution.review_decision or "uncertain",
                rationale=resolution.review_rationale or "ingest_group_resolution",
            )
        for temporal_review_candidate in resolution.temporal_review_candidates:
            _quarantine_temporal_pair(
                claims,
                temporal_review_candidate,
                claim,
                now,
                rationale=resolution.review_rationale or "temporal_update_uncertain",
                id_factory=new_id,
            )

    @staticmethod
    def _converge_superseded_members(
        claims: ClaimRepository,
        claim: dict[str, Any],
        now: str,
        resolution: _IngestResolution,
    ) -> None:
        tip = resolution.temporal_backfill_tip
        winner = tip or claim
        member_ids = (str(claim["id"]),) if tip else resolution.superseded_member_ids
        for member_id in member_ids:
            superseded = claims.supersede_with_inline(
                member_id,
                str(winner["id"]),
                winner.get("value"),
                str(winner["valid_from"]),
                now,
                commit=False,
            )
            if not superseded.applied:
                raise ConflictError(f"claim changed during ingest state convergence: {member_id}")
        if member_ids and tip is None:
            assert_transition("candidate", "active")
            if not claims.update_status(claim["id"], "active", commit=False):
                raise ConflictError(f"new state-change claim disappeared during ingest: {claim['id']}")


def _slot(value: str | None, q: dict[str, Any], trusted: str | None) -> str | None:
    if value == trusted == "config.version":
        return value
    return validate_slot_instance(value, q)


def _build_claim_drafts(
    extracted: ExtractedClaim,
    event: dict[str, Any],
    now: str,
    embedder: EmbedderProtocol,
    authority: str | None,
    policy: TTLPolicy,
    index_text_mode: IndexTextMode,
    trusted_projector_slot: str | None,
) -> _ClaimDraft | StoreClaimResult:
    """阶段 1：规范化提取结果、计算 TTL 并生成 claim 草稿。"""
    # Claim 的实体、去重与冲突身份由 (namespace_key, subject_entity_id) 共同确定。
    # 其他多租户安全边界仍需由部署层统一约束，不能仅依赖此处的 namespace。
    namespace = event.get("tenant_id", "default")
    original_subject = extracted.subject
    subject = normalize_entity_id(original_subject)
    invalid_reason = invalid_subject_reason(original_subject)
    replacement: str | None = None
    if invalid_reason is not None:
        for entity in getattr(extracted, "entities", None) or []:
            candidate = normalize_entity_id(entity)
            if invalid_subject_reason(entity) is None:
                replacement = candidate
                break
        subject = replacement or isolated_subject_id(
            event.get("id"), original_subject, extracted.predicate, extracted.value
        )
        current_audit().emit(
            "ingest",
            "subject_guard",
            "replaced" if replacement else "isolated",
            detail={
                "original_subject": original_subject,
                "normalized_subject": normalize_entity_id(original_subject),
                "replacement_subject": subject,
                "reason_code": invalid_reason,
                "isolation_reason": None if replacement else "invalid_subject_isolated",
            },
        )
    qualifiers = extracted.qualifiers or {}
    canonical_attribute = validate_canonical_attribute(
        extracted.predicate, getattr(extracted, "canonical_attribute", None)
    )
    predicate = predicate_for_canonical_attribute(canonical_attribute, extracted.predicate)
    if predicate != extracted.predicate:
        current_audit().emit(
            "ingest",
            "predicate_normalized",
            "changed",
            detail={
                "llm_predicate": extracted.predicate,
                "normalized_predicate": predicate,
                "canonical_attribute": canonical_attribute,
                "reason_code": "canonical_attribute_projection",
            },
        )
    requested_slot = getattr(extracted, "canonical_slot", None)
    canonical_slot = _slot(requested_slot, qualifiers, trusted_projector_slot)
    if requested_slot and canonical_slot is None:
        current_audit().emit(
            "ingest",
            "slot_instance_validation",
            "downgraded",
            detail={
                "requested_slot": requested_slot,
                "reason": "missing_required_qualifier",
            },
        )
    topic_tags = normalize_topic_tags(getattr(extracted, "topic_tags", None))
    scope = extracted.scope if extracted.scope in {"temporal", "permanent"} else "permanent"
    try:
        importance = min(1.0, max(0.0, float(extracted.importance)))
    except (TypeError, ValueError):
        importance = 0.5
    protected = predicate == "explicit_memory" or canonical_attribute in {
        "memory.explicit",
        "identity.name",
    }
    if importance < policy.importance_write_floor and not protected:
        return StoreClaimResult(None, "skipped", "importance_below_write_floor")
    observed_at = normalize_utc_iso(str(event.get("occurred_at", now)), "observed_at")
    recorded_from = normalize_utc_iso(now, "recorded_from")
    memory_layer = getattr(extracted, "memory_layer", "durable")
    retention_anchor = _retention_anchor(
        observed_at,
        recorded_from,
        memory_layer=memory_layer,
        is_plan=canonical_attribute.startswith("plan.") or predicate == "计划",
        occurred_start=getattr(extracted, "occurred_start", None),
        occurred_end=getattr(extracted, "occurred_end", None),
    )
    expires_at, _expiration_reason = compute_expiration(
        scope=scope,
        importance=importance,
        volatility=extracted.volatility,
        canonical_slot=canonical_slot,
        valid_to=None,
        observed_at=retention_anchor,
        recorded_from=recorded_from,
        policy=policy,
        canonical_attribute=canonical_attribute,
    )
    claim = {
        "id": new_id(),
        "namespace_key": namespace,
        "subject_entity_id": subject,
        "predicate": predicate,
        "value": extracted.value,
        "canonical_attribute": canonical_attribute,
        "canonical_slot": canonical_slot,
        "topic_tags_json": json.dumps(topic_tags, ensure_ascii=False, separators=(",", ":")),
        "occurred_start": getattr(extracted, "occurred_start", None) or None,
        "occurred_end": getattr(extracted, "occurred_end", None) or None,
        "entities_json": (
            json.dumps(
                getattr(extracted, "entities"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if getattr(extracted, "entities", None)
            else None
        ),
        "fact_hash": compute_fact_hash(subject, predicate, extracted.value),
        "qualifiers": qualifiers,
        "conflict_key": compute_conflict_key(
            namespace,
            subject,
            predicate,
            canonical_slot,
            qualifiers,
            version=4,
        ),
        "conflict_key_version": 4,
        "legacy_conflict_key": compute_legacy_conflict_key(namespace, subject, predicate, qualifiers),
        "valid_from": observed_at,
        "recorded_from": recorded_from,
        "observed_at": observed_at,
        "expires_at": expires_at,
        "volatility": extracted.volatility,
        "assertion_kind": (
            extracted.assertion_kind
            if extracted.assertion_kind in {"unknown", "observation", "inference"}
            else "unknown"
        ),
        "status": "active",
        "confidence": extracted.confidence,
        "scope": scope,
        "importance": importance,
        "access_count": 0,
        "last_accessed_at": None,
        "source_authority": authority or ("low" if event.get("actor_type") == "assistant" else "medium"),
        "extractor_version": event.get("extractor_version")
        or ("llm-v2" if event.get("extractor") == "llm" else "fake-v1"),
        "embedding_model": getattr(embedder, "model", "fake"),
        "embedding_dim": embedder.dim,
    }
    claim["index_text"] = build_index_text({**claim, "topic_tags": topic_tags}, mode=index_text_mode)
    claim["embedding_dense"] = embedder.embed_one(claim_text(claim))
    return _ClaimDraft(claim, qualifiers)


def _insert_pending_dedup_pair(
    connection: sqlite3.Connection,
    existing_claim_id: str,
    new_claim: dict[str, Any],
    similarity: float,
    created_at: str,
    *,
    metrics: AdmissionMetrics = DEFAULT_ADMISSION_METRICS,
) -> bool:
    """Record an LLM gray-area pair without making a remote call in the write transaction."""
    return _insert_pending_dedup_pair_row(
        connection,
        existing_claim_id,
        new_claim,
        similarity,
        created_at,
        metrics=metrics,
        id_factory=new_id,
        settings_factory=Settings,
    )
