"""把历史 persona subject 归一为 namespace 内的 ``user`` 标签。"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.domain.entity import PERSONA_ENTITY_ALIASES, normalize_entity_alias
from hl_mem.recall.lexicalizer import prepare_fts_document
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2

LEGACY_DATA_MIGRATION_VERSION = "038_data_subject_canonicalization_v1"
DATA_MIGRATION_VERSION = "038_data_subject_canonicalization_v2"
LOGGER = logging.getLogger(__name__)


def _decode_json(claim_id: str, field: str, raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"claim {claim_id} has invalid {field}") from error


def _canonical_index_text(index_text: Any, old_subject: str, new_subject: str) -> Any:
    if not isinstance(index_text, str):
        return index_text
    if index_text == old_subject:
        return new_subject
    for separator in ("：", " "):
        prefix = f"{old_subject}{separator}"
        if index_text.startswith(prefix):
            return f"{new_subject}{separator}{index_text[len(prefix):]}"
    return index_text


def _canonical_entities(claim_id: str, raw: Any) -> Any:
    """Canonicalize explicit persona strings while preserving other entity payloads."""
    entities = _decode_json(claim_id, "entities_json", raw, None)
    if not isinstance(entities, list):
        return raw
    canonical = [
        normalize_entity_alias(entity, aliases=PERSONA_ENTITY_ALIASES) if isinstance(entity, str) else entity
        for entity in entities
    ]
    if canonical == entities:
        return raw
    return json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))


def _active_collision_stats(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    """Return exact-duplicate and conflicting active group/excess-row counts."""
    duplicate_groups = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(claim_count - 1),0) FROM ("
        "SELECT COUNT(*) AS claim_count FROM claims "
        "WHERE status='active' AND fact_hash IS NOT NULL "
        "GROUP BY namespace_key,fact_hash HAVING COUNT(*)>1)"
    ).fetchone()
    conflict_groups = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(claim_count),0) FROM ("
        "SELECT COUNT(*) AS claim_count FROM claims "
        "WHERE status='active' AND conflict_key IS NOT NULL "
        "GROUP BY namespace_key,conflict_key HAVING COUNT(DISTINCT fact_hash)>1)"
    ).fetchone()
    return (
        int(duplicate_groups[0]),
        int(duplicate_groups[1]),
        int(conflict_groups[0]),
        int(conflict_groups[1]),
    )


def backfill_subject_canonicalization(connection: sqlite3.Connection) -> int:
    """只迁移明确 persona 别名，并在持写锁的单一事务内同步/失效派生数据。"""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?",
        (DATA_MIGRATION_VERSION,),
    ).fetchone():
        return 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (DATA_MIGRATION_VERSION,),
        ).fetchone():
            connection.commit()
            return 0
        legacy_backfill_applied = (
            connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                (LEGACY_DATA_MIGRATION_VERSION,),
            ).fetchone()
            is not None
        )
        rows = connection.execute(
            "SELECT rowid,id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
            "canonical_slot,index_text,entities_json FROM claims ORDER BY id"
        ).fetchall()
        updated = 0
        for row in rows:
            claim = dict(row)
            old_subject = str(claim["subject_entity_id"] or "")
            new_subject = normalize_entity_alias(old_subject, aliases=PERSONA_ENTITY_ALIASES)
            if new_subject != "user":
                continue
            if new_subject == old_subject and not legacy_backfill_applied:
                continue
            value = _decode_json(str(claim["id"]), "value_json", claim["value_json"], None)
            qualifiers = _decode_json(str(claim["id"]), "qualifiers_json", claim["qualifiers_json"], {})
            if not isinstance(qualifiers, dict):
                raise ValueError(f"claim {claim['id']} qualifiers_json must be an object")
            namespace = str(claim["namespace_key"] or "default")
            predicate = str(claim["predicate"] or "")
            index_text = _canonical_index_text(claim["index_text"], old_subject, new_subject)
            entities_json = _canonical_entities(str(claim["id"]), claim["entities_json"])
            fact_hash = compute_fact_hash_v2(new_subject, predicate, value)
            conflict_key = compute_conflict_key(
                namespace,
                new_subject,
                predicate,
                claim["canonical_slot"],
                qualifiers,
            )
            connection.execute(
                "UPDATE claims SET subject_entity_id=?,fact_hash=?,conflict_key=?,index_text=?,entities_json=?,"
                "embedding_dense=NULL,embedding_sparse=NULL,embedding_model=NULL,embedding_dim=NULL WHERE id=?",
                (new_subject, fact_hash, conflict_key, index_text, entities_json, claim["id"]),
            )
            connection.execute(
                "INSERT INTO claim_vector_dirty(claim_id,reason,queued_at) "
                "VALUES(?,'subject_canonicalization',CURRENT_TIMESTAMP) "
                "ON CONFLICT(claim_id) DO UPDATE SET "
                "reason='subject_canonicalization',queued_at=CURRENT_TIMESTAMP",
                (claim["id"],),
            )
            connection.execute("DELETE FROM claims_fts_v2 WHERE rowid=?", (claim["rowid"],))
            connection.execute(
                "INSERT INTO claims_fts_v2(rowid,terms) VALUES(?,?)",
                (claim["rowid"], prepare_fts_document(index_text or "")),
            )
            updated += 1
        duplicate_group_count, duplicate_row_count, conflict_group_count, conflict_row_count = _active_collision_stats(
            connection
        )
        connection.execute(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            (DATA_MIGRATION_VERSION,),
        )
        connection.commit()
        log = LOGGER.warning if duplicate_group_count or conflict_group_count else LOGGER.info
        log(
            "subject canonicalization migration completed: updated=%d duplicate_groups=%d "
            "duplicate_rows=%d conflict_groups=%d conflict_rows=%d",
            updated,
            duplicate_group_count,
            duplicate_row_count,
            conflict_group_count,
            conflict_row_count,
        )
        return updated
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
