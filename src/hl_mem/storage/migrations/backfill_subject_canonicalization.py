"""把历史 persona subject 归一为 namespace 内的 ``user`` 标签。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.domain.entity import PERSONA_ENTITY_ALIASES, normalize_entity_alias
from hl_mem.recall.lexicalizer import prepare_fts_document
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2

DATA_MIGRATION_VERSION = "038_data_subject_canonicalization_v1"


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


def backfill_subject_canonicalization(connection: sqlite3.Connection) -> int:
    """只迁移明确 persona 别名，并在单一事务内同步派生键和 FTS。"""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?",
        (DATA_MIGRATION_VERSION,),
    ).fetchone():
        return 0

    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT rowid,id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
            "canonical_slot,index_text FROM claims ORDER BY id"
        ).fetchall()
        updated = 0
        for row in rows:
            claim = dict(row)
            old_subject = str(claim["subject_entity_id"] or "")
            new_subject = normalize_entity_alias(old_subject, aliases=PERSONA_ENTITY_ALIASES)
            if new_subject == old_subject:
                continue
            if new_subject != "user":
                continue
            value = _decode_json(str(claim["id"]), "value_json", claim["value_json"], None)
            qualifiers = _decode_json(str(claim["id"]), "qualifiers_json", claim["qualifiers_json"], {})
            if not isinstance(qualifiers, dict):
                raise ValueError(f"claim {claim['id']} qualifiers_json must be an object")
            namespace = str(claim["namespace_key"] or "default")
            predicate = str(claim["predicate"] or "")
            index_text = _canonical_index_text(claim["index_text"], old_subject, new_subject)
            fact_hash = compute_fact_hash_v2(new_subject, predicate, value)
            conflict_key = compute_conflict_key(
                namespace,
                new_subject,
                predicate,
                claim["canonical_slot"],
                qualifiers,
            )
            connection.execute(
                "UPDATE claims SET subject_entity_id=?,fact_hash=?,conflict_key=?,index_text=? WHERE id=?",
                (new_subject, fact_hash, conflict_key, index_text, claim["id"]),
            )
            connection.execute("DELETE FROM claims_fts_v2 WHERE rowid=?", (claim["rowid"],))
            connection.execute(
                "INSERT INTO claims_fts_v2(rowid,terms) VALUES(?,?)",
                (claim["rowid"], prepare_fts_document(index_text or "")),
            )
            updated += 1
        connection.execute(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            (DATA_MIGRATION_VERSION,),
        )
        connection.commit()
        return updated
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
