from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from hl_mem.application.entity_resolution import EntityResolutionService, SubjectResolution, v4_conflict_key
from hl_mem.storage.entities import EntityRepository

COLLISION_SCAN_LIMIT = 500


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


def prepare_canonical_entity_audit_clone(connection: sqlite3.Connection, now: str, namespace: str = "default") -> None:
    EntityRepository(connection).seed_builtins(namespace, now=now)
    connection.commit()


def _collision_outcome(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    resolution: SubjectResolution,
    proposed_key: str | None,
) -> str:
    if proposed_key is None or (canonical_id := resolution.canonical_entity_id) is None:
        return resolution.outcome
    if connection.execute(
        "SELECT 1 FROM claims WHERE id<>? AND namespace_key=? AND conflict_key=? "
        "AND status IN ('active','candidate','disputed') LIMIT 1",
        (row["id"], row["namespace_key"], proposed_key),
    ).fetchone():
        return "collision"
    candidates = connection.execute(
        "SELECT DISTINCT claim.* FROM claims AS claim JOIN entity_aliases AS alias "
        "ON alias.namespace_key=claim.namespace_key AND alias.alias_normalized="
        "hl_mem_normalize_alias(claim.subject_entity_id) JOIN canonical_entities AS entity "
        "ON entity.namespace_key=alias.namespace_key AND entity.id=alias.canonical_entity_id "
        "WHERE claim.id<>? AND claim.namespace_key=? "
        "AND claim.canonical_slot IS ? AND claim.status IN ('active','candidate','disputed') "
        "AND alias.valid_to IS NULL AND entity.status='active' AND alias.canonical_entity_id=? "
        "AND alias.source_kind IN ('builtin','config_explicit','user_explicit','migration_exact') "
        "ORDER BY claim.id LIMIT ?",
        (row["id"], row["namespace_key"], row["canonical_slot"], canonical_id, COLLISION_SCAN_LIMIT + 1),
    ).fetchall()
    for candidate in candidates[:COLLISION_SCAN_LIMIT]:
        if v4_conflict_key(candidate, str(canonical_id)) == proposed_key:
            return "collision"
    return "overflow" if len(candidates) > COLLISION_SCAN_LIMIT else resolution.outcome


def audit_canonical_entity_backfill(
    connection: sqlite3.Connection,
    *,
    cursor: str | None = None,
    limit: int = 100,
) -> BackfillAuditBatch:
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
        proposed_key = (
            v4_conflict_key(row, resolution.canonical_entity_id) if resolution.canonical_entity_id is not None else None
        )
        outcome = _collision_outcome(connection, row, resolution, proposed_key)
        records.append(BackfillAuditRecord(str(row["id"]), outcome, resolution.canonical_entity_id, proposed_key))
    next_cursor = str(rows[min(limit, len(rows)) - 1]["id"]) if records else cursor
    return BackfillAuditBatch(tuple(records), next_cursor, len(rows) <= limit)
