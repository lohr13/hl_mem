"""Bounded, read-only-by-default audit for canonical subject projection."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace

from hl_mem.application.entity_resolution import EntityResolutionService
from hl_mem.domain.claims.conflicts import compute_conflict_key


@dataclass(frozen=True)
class BackfillAuditRecord:
    claim_id: str
    outcome: str
    canonical_entity_id: str | None
    proposed_conflict_key: str | None


@dataclass(frozen=True)
class BackfillAuditBatch:
    records: tuple[BackfillAuditRecord, ...]
    next_cursor: str | None
    done: bool


def audit_canonical_entity_backfill(
    connection: sqlite3.Connection,
    *,
    cursor: str | None = None,
    limit: int = 100,
) -> BackfillAuditBatch:
    """Return one stable audit page without changing the database."""

    if not 1 <= limit <= 500:
        raise ValueError("entity backfill limit must be between 1 and 500")
    rows = connection.execute(
        "SELECT id,namespace_key,subject_entity_id,predicate,canonical_slot,qualifiers_json "
        "FROM claims WHERE id>? AND subject_canonical_entity_id IS NULL ORDER BY id LIMIT ?",
        (cursor or "", limit + 1),
    ).fetchall()
    service = EntityResolutionService(connection)
    records: list[BackfillAuditRecord] = []
    for row in rows[:limit]:
        resolution = service.resolve_subject(str(row["namespace_key"]), str(row["subject_entity_id"] or ""))
        proposed_key = None
        outcome = resolution.outcome
        if resolution.canonical_entity_id is not None:
            proposed_key = compute_conflict_key(
                str(row["namespace_key"]),
                str(row["subject_entity_id"] or ""),
                str(row["predicate"] or ""),
                row["canonical_slot"],
                json.loads(row["qualifiers_json"] or "{}"),
                version=4,
                subject_canonical_entity_id=resolution.canonical_entity_id,
            )
            if (
                proposed_key
                and connection.execute(
                    "SELECT 1 FROM claims WHERE id<>? AND namespace_key=? AND conflict_key=? "
                    "AND status IN ('active','candidate','disputed') LIMIT 1",
                    (row["id"], row["namespace_key"], proposed_key),
                ).fetchone()
            ):
                outcome = "collision"
        records.append(BackfillAuditRecord(str(row["id"]), outcome, resolution.canonical_entity_id, proposed_key))
    proposed_counts = Counter(record.proposed_conflict_key for record in records if record.proposed_conflict_key)
    records = [
        (
            replace(record, outcome="collision")
            if record.proposed_conflict_key and proposed_counts[record.proposed_conflict_key] > 1
            else record
        )
        for record in records
    ]
    next_cursor = str(rows[min(limit, len(rows)) - 1]["id"]) if records else cursor
    return BackfillAuditBatch(tuple(records), next_cursor, len(rows) <= limit)
