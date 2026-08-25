"""Coordinate projection glue kept outside the atomic ingest hot spot."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from hl_mem.application.entity_resolution import EntityResolutionService, SubjectResolution
from hl_mem.domain.instruments import InstrumentTarget
from hl_mem.domain.plan_fulfillment import is_result_claim


@dataclass(slots=True)
class IngestCoordinateProjection:
    claim: dict[str, Any]
    service: EntityResolutionService
    subject: SubjectResolution
    target: InstrumentTarget
    target_enforced: bool
    audit_event: tuple[tuple[Any, ...], dict[str, Any]]

    @classmethod
    def prepare(
        cls,
        connection: sqlite3.Connection,
        claim: dict[str, Any],
        now: str,
        price_mode_override: str | None,
        event_id: str,
    ) -> "IngestCoordinateProjection":
        service, subject = EntityResolutionService.prepare_ingest(connection, claim, now)
        configured = getattr(connection, "hl_mem_settings", None)
        mode = price_mode_override or getattr(configured, "price_target_mode", "observe")
        target = service.resolve_instrument_target(str(claim["namespace_key"]), str(claim.get("value") or ""))
        enforced = bool(mode == "enforce" and target.outcome == "resolved" and target.alias_version is not None)
        if enforced:
            claim["canonical_target_entity_id"] = target.canonical_entity_id
        audit_event = (
            ("ingest", "instrument_target", "applied" if enforced else target.outcome),
            {
                "event_id": event_id,
                "claim_id": claim["id"],
                "detail": {
                    "mode": mode,
                    "canonical_target_entity_id": target.canonical_entity_id,
                    "source": target.source,
                },
            },
        )
        return cls(claim, service, subject, target, enforced, audit_event)

    def persist_and_queue(self, result_id: str, now: str) -> None:
        self.service.link_subject(str(self.claim["id"]), self.subject)
        if self.target_enforced:
            self.service.link_target(str(self.claim["id"]), self.target)
        configured = getattr(self.service.connection, "hl_mem_settings", None)
        if getattr(configured, "plan_fulfillment_mode", "audit") != "off" and is_result_claim(self.claim):
            from hl_mem.workers.plan_fulfillment import enqueue_plan_result

            enqueue_plan_result(self.service.connection, result_id, now, commit=False)
