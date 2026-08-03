"""显式记忆纠正服务。原子写入纠正事件、替代 Claim 与证据链。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from hl_mem.application.ingest import compute_fact_hash, new_id
from hl_mem.domain.claims.claim import build_index_text
from hl_mem.domain.claims.retention import compute_expiration, normalize_utc_iso
from hl_mem.errors import ConflictError, NotFoundError, ValidationError
from hl_mem.lifecycle import assert_transition
from hl_mem.protocols import EmbedderProtocol
from hl_mem.recall.recall_pipeline import stale_observations
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CorrectionService:
    """执行不经过 extractor 的显式内容替换或撤回。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        embedder: EmbedderProtocol,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.connection = connection
        self.embedder = embedder
        self.settings = settings or Settings()

    def apply(
        self,
        memory_id: Any,
        *,
        action: Any,
        corrected_text: Any,
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        """按显式动作选择内容替换或撤回，并返回统一事件字段。"""
        if action == "replace":
            return self.correct(memory_id, corrected_text or "", idempotency_key)
        if action == "retract":
            return self._retract(memory_id, idempotency_key)
        raise ValidationError(f"unsupported correction action: {action}")

    def correct(self, memory_id: Any, corrected_text: Any, idempotency_key: Any = None) -> dict[str, Any]:
        """只替换 Claim 内容，继承分类字段并重建 hash、索引、向量与 TTL。"""
        idempotency_key = self._validate_input(memory_id, corrected_text, idempotency_key)
        existing = self._existing_result(memory_id, "replace", corrected_text, idempotency_key)
        if existing is not None:
            return existing

        repository = ClaimRepository(self.connection)
        old_claim = repository.get_claim(memory_id)
        if old_claim is None:
            raise NotFoundError(f"memory not found: {memory_id}")
        assert_transition(str(old_claim["status"]), "superseded")
        inherited_snapshot = self._inherited_snapshot(old_claim)
        timestamp = normalize_utc_iso(_now(), "corrected_at")
        new_claim_id = new_id()
        new_claim = self._replacement_claim(old_claim, new_claim_id, corrected_text, timestamp)
        correction_event_id = new_id()

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._existing_result(memory_id, "replace", corrected_text, idempotency_key)
            if existing is not None:
                self.connection.commit()
                return existing
            current = repository.get_claim(memory_id)
            if current is None:
                raise NotFoundError(f"memory not found: {memory_id}")
            assert_transition(str(current["status"]), "superseded")
            if self._inherited_snapshot(current) != inherited_snapshot:
                raise ConflictError(f"memory changed during correction: {memory_id}")
            self._insert_event(
                event_id=correction_event_id,
                idempotency_key=idempotency_key,
                namespace=str(current["namespace_key"]),
                memory_id=memory_id,
                action="replace",
                corrected_text=corrected_text,
                new_claim_id=new_claim_id,
                timestamp=timestamp,
            )
            if not repository.insert_claim(new_claim, commit=False):
                raise ConflictError(f"replacement claim already exists: {new_claim_id}")
            cursor = self.connection.execute(
                "UPDATE claims SET status='superseded',valid_to=?,recorded_to=?,superseded_by_id=? "
                "WHERE id=? AND status='active'",
                (timestamp, timestamp, new_claim_id, memory_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"memory is no longer active: {memory_id}")
            stale_observations(self.connection, memory_id, commit=False)
            evidence = EvidenceRepository(self.connection)
            evidence.add_link(
                {
                    "id": new_id(),
                    "derived_type": "claim",
                    "derived_id": new_claim_id,
                    "evidence_type": "claim",
                    "evidence_id": memory_id,
                    "relation": "supersedes",
                    "weight": 1.0,
                },
                commit=False,
            )
            evidence.add_link(
                {
                    "id": new_id(),
                    "derived_type": "claim",
                    "derived_id": new_claim_id,
                    "evidence_type": "event",
                    "evidence_id": correction_event_id,
                    "relation": "derived_from",
                    "weight": 1.0,
                },
                commit=False,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {
            "correction_event_id": correction_event_id,
            "new_claim_id": new_claim_id,
            "created": True,
            "id": memory_id,
            "replacement_event_id": correction_event_id,
        }

    def _replacement_claim(
        self,
        old_claim: dict[str, Any],
        new_claim_id: str,
        corrected_text: str,
        timestamp: str,
    ) -> dict[str, Any]:
        topic_tags = list(old_claim.get("topic_tags") or [])
        scope = str(old_claim.get("scope") or "permanent")
        importance = float(old_claim.get("importance", 0.5))
        volatility = str(old_claim.get("volatility") or "stable")
        expires_at, _ = compute_expiration(
            scope=scope,
            importance=importance,
            volatility=volatility,
            canonical_slot=old_claim.get("canonical_slot"),
            valid_to=None,
            observed_at=timestamp,
            recorded_from=timestamp,
            policy=self.settings.retention_policy(),
            canonical_attribute=old_claim.get("canonical_attribute"),
        )
        claim = {
            "id": new_claim_id,
            "namespace_key": old_claim["namespace_key"],
            "subject_entity_id": old_claim.get("subject_entity_id"),
            "predicate": old_claim.get("predicate"),
            "value": corrected_text,
            "qualifiers": dict(old_claim.get("qualifiers") or {}),
            "canonical_attribute": old_claim.get("canonical_attribute"),
            "canonical_slot": old_claim.get("canonical_slot"),
            "topic_tags_json": json.dumps(topic_tags, ensure_ascii=False, separators=(",", ":")),
            "occurred_start": old_claim.get("occurred_start"),
            "occurred_end": old_claim.get("occurred_end"),
            "entities_json": (
                json.dumps(old_claim["entities"], ensure_ascii=False, separators=(",", ":"))
                if old_claim.get("entities") is not None
                else None
            ),
            "fact_hash": compute_fact_hash(
                str(old_claim.get("subject_entity_id") or ""),
                str(old_claim.get("predicate") or ""),
                corrected_text,
            ),
            "conflict_key": old_claim.get("conflict_key"),
            "conflict_key_version": old_claim.get("conflict_key_version", 3),
            "legacy_conflict_key": old_claim.get("legacy_conflict_key"),
            "valid_from": timestamp,
            "recorded_from": timestamp,
            "observed_at": timestamp,
            "expires_at": expires_at,
            "volatility": volatility,
            "status": "active",
            "confidence": old_claim.get("confidence", 0.5),
            "importance": importance,
            "scope": scope,
            "access_count": 0,
            "last_accessed_at": None,
            "last_decayed_at": None,
            "source_authority": old_claim.get("source_authority") or "medium",
            "supersedes_id": old_claim["id"],
            "extractor_version": "correction-v1",
            "embedding_model": getattr(self.embedder, "model", "unknown"),
            "embedding_dim": self.embedder.dim,
        }
        claim["index_text"] = build_index_text(
            {**claim, "topic_tags": topic_tags},
            mode=self.settings.index_text_mode,
        )
        claim["embedding_dense"] = self.embedder.embed_one(str(claim["index_text"]))
        return claim

    @staticmethod
    def _inherited_snapshot(claim: dict[str, Any]) -> dict[str, Any]:
        """冻结纠正必须继承的字段，用于远程 embedding 后的乐观并发校验。"""
        return {
            key: claim.get(key)
            for key in (
                "namespace_key",
                "subject_entity_id",
                "predicate",
                "value",
                "qualifiers",
                "canonical_attribute",
                "canonical_slot",
                "topic_tags",
                "occurred_start",
                "occurred_end",
                "entities",
                "conflict_key",
                "conflict_key_version",
                "legacy_conflict_key",
                "volatility",
                "confidence",
                "importance",
                "scope",
                "source_authority",
            )
        }

    def _retract(self, memory_id: str, idempotency_key: Any = None) -> dict[str, Any]:
        idempotency_key = self._validate_input(memory_id, None, idempotency_key)
        existing = self._existing_result(memory_id, "retract", None, idempotency_key)
        if existing is not None:
            return existing
        repository = ClaimRepository(self.connection)
        claim = repository.get_claim(memory_id)
        if claim is None:
            raise NotFoundError(f"memory not found: {memory_id}")
        assert_transition(str(claim["status"]), "retracted")
        timestamp = normalize_utc_iso(_now(), "corrected_at")
        event_id = new_id()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._existing_result(memory_id, "retract", None, idempotency_key)
            if existing is not None:
                self.connection.commit()
                return existing
            current = repository.get_claim(memory_id)
            if current is None:
                raise NotFoundError(f"memory not found: {memory_id}")
            assert_transition(str(current["status"]), "retracted")
            self._insert_event(
                event_id=event_id,
                idempotency_key=idempotency_key,
                namespace=str(current["namespace_key"]),
                memory_id=memory_id,
                action="retract",
                corrected_text=None,
                new_claim_id=None,
                timestamp=timestamp,
            )
            cursor = self.connection.execute(
                "UPDATE claims SET status='retracted',embedding_dense=NULL,embedding_sparse=NULL WHERE id=?",
                (memory_id,),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"memory not found: {memory_id}")
            stale_observations(self.connection, memory_id, commit=False)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {
            "correction_event_id": event_id,
            "memory_id": memory_id,
            "action": "retract",
            "forgotten": True,
            "created": True,
            "id": memory_id,
        }

    def _existing_result(
        self,
        memory_id: str,
        action: Literal["replace", "retract"],
        corrected_text: str | None,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        events = EventRepository(self.connection)
        event_id = events.find_id_by_idempotency_key(idempotency_key)
        if event_id is None:
            return None
        event = events.get_event(event_id)
        if event is None:
            raise RuntimeError(f"idempotent correction event disappeared: {event_id}")
        content = event.get("content")
        expected_type = "correction" if action == "replace" else "feedback"
        if (
            event.get("event_type") != expected_type
            or not isinstance(content, dict)
            or content.get("memory_id") != memory_id
            or content.get("action") != action
            or (content.get("corrected_text") or None) != corrected_text
        ):
            raise ConflictError(f"idempotency key {idempotency_key!r} was already used with a different correction")
        if action == "replace":
            new_claim_id = content.get("new_claim_id")
            if not isinstance(new_claim_id, str) or not new_claim_id:
                raise ConflictError(f"idempotent correction event has no replacement claim: {event_id}")
            return {
                "correction_event_id": event_id,
                "new_claim_id": new_claim_id,
                "created": False,
                "id": memory_id,
                "replacement_event_id": event_id,
                "idempotent": True,
            }
        return {
            "correction_event_id": event_id,
            "memory_id": memory_id,
            "action": "retract",
            "forgotten": True,
            "created": False,
            "id": memory_id,
            "idempotent": True,
        }

    def _insert_event(
        self,
        *,
        event_id: str,
        idempotency_key: str,
        namespace: str,
        memory_id: str,
        action: Literal["replace", "retract"],
        corrected_text: str | None,
        new_claim_id: str | None,
        timestamp: str,
    ) -> None:
        content = {
            "memory_id": memory_id,
            "action": action,
            "corrected_text": corrected_text,
        }
        if new_claim_id is not None:
            content["new_claim_id"] = new_claim_id
        created = EventRepository(self.connection).insert_event(
            {
                "id": event_id,
                "idempotency_key": idempotency_key,
                "tenant_id": namespace,
                "event_type": "correction" if action == "replace" else "feedback",
                "actor_type": "user",
                "content": content,
                "occurred_at": timestamp,
                "recorded_at": timestamp,
            },
            commit=False,
        )
        if not created:
            raise ConflictError(f"correction event already exists: {event_id}")

    @staticmethod
    def _validate_input(memory_id: Any, corrected_text: Any, idempotency_key: Any) -> str:
        if not isinstance(memory_id, str):
            raise ValidationError("memory_id must be a string")
        if not memory_id:
            raise ValidationError("memory_id is required")
        if corrected_text is not None:
            if not isinstance(corrected_text, str):
                raise ValidationError("corrected_text must be a string")
            if not corrected_text.strip():
                raise ValidationError("corrected_text is required for replace")
            if len(corrected_text) > 50000:
                raise ValidationError("corrected_text must be at most 50000 characters")
        if idempotency_key is None:
            return new_id()
        if not isinstance(idempotency_key, str):
            raise ValidationError("idempotency_key must be a string")
        if not idempotency_key:
            raise ValidationError("idempotency_key is required")
        if len(idempotency_key) > 200:
            raise ValidationError("idempotency_key must be at most 200 characters")
        return idempotency_key
