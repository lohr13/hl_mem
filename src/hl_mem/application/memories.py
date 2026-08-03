"""记忆分页查询应用服务。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hl_mem.errors import NotFoundError
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository


class MemoryQueryService:
    """通过统一仓储路径查询可公开展示的 Claim 页。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_memories(
        self,
        *,
        namespace: str = "default",
        status: str = "active",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        claims, total = ClaimRepository(self.connection).list_memories(namespace, status, limit, offset)
        return {
            "memories": [self._public_item(claim) for claim in claims],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        """返回 Claim 内容、来源事件、替代关系与冲突历史。"""
        claim = ClaimRepository(self.connection).get_claim(memory_id)
        if claim is None:
            raise NotFoundError(f"memory not found: {memory_id}")
        links = sorted(
            EvidenceRepository(self.connection).get_links_for_derived("claim", memory_id),
            key=lambda link: str(link["id"]),
        )
        events = EventRepository(self.connection)
        source_events = []
        for link in links:
            if link["evidence_type"] != "event":
                continue
            event = events.get_event(str(link["evidence_id"]))
            if event is not None:
                source_events.append(self._public_event(event))
        conflicts = [
            dict(row)
            for row in self.connection.execute(
                "SELECT id,left_claim_id,right_claim_id,status,decision,rationale,confidence,created_at,resolved_at "
                "FROM conflict_cases WHERE left_claim_id=? OR right_claim_id=? ORDER BY created_at,id",
                (memory_id, memory_id),
            ).fetchall()
        ]
        return {
            "id": str(claim["id"]),
            "text": self._display_text(claim.get("value")),
            "namespace": str(claim["namespace_key"]),
            "subject": claim.get("subject_entity_id"),
            "predicate": claim.get("predicate"),
            "qualifiers": dict(claim.get("qualifiers") or {}),
            "status": str(claim["status"]),
            "confidence": claim.get("confidence"),
            "importance": claim.get("importance"),
            "scope": claim.get("scope"),
            "recorded_from": str(claim["recorded_from"]),
            "recorded_to": claim.get("recorded_to"),
            "valid_from": claim.get("valid_from"),
            "valid_to": claim.get("valid_to"),
            "expires_at": claim.get("expires_at"),
            "canonical_attribute": claim.get("canonical_attribute"),
            "canonical_slot": claim.get("canonical_slot"),
            "topic_tags": list(claim.get("topic_tags") or []),
            "superseded_by_id": claim.get("superseded_by_id"),
            "evidence_links": links,
            "source_events": source_events,
            "conflicts": conflicts,
        }

    @staticmethod
    def _display_text(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _public_event(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(event["id"]),
            "event_type": str(event["event_type"]),
            "actor_type": str(event["actor_type"]),
            "content": event.get("content") or {},
            "occurred_at": str(event["occurred_at"]),
            "recorded_at": str(event["recorded_at"]),
            "source_uri": event.get("source_uri"),
        }

    @staticmethod
    def _public_item(claim: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(claim["id"]),
            "text": MemoryQueryService._display_text(claim.get("value")),
            "status": str(claim["status"]),
            "recorded_from": str(claim["recorded_from"]),
            "valid_from": claim.get("valid_from"),
            "canonical_slot": claim.get("canonical_slot"),
            "topic_tags": list(claim.get("topic_tags") or []),
        }
