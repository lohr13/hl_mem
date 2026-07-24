"""使用 v006 不可变快照回填 canonical attribute 与 conflict key。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hl_mem.storage.migrations.snapshots.v006_snapshot import (
    compute_conflict_key,
    compute_legacy_conflict_key,
    infer_canonical_attribute,
)


DATA_MIGRATION_VERSION = "006_data_conflict_key_v2"


def _decode_qualifiers(claim_id: str, raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError as error:
        raise ValueError(f"claim {claim_id} has invalid qualifiers_json") from error
    if not isinstance(value, dict):
        raise ValueError(f"claim {claim_id} qualifiers_json must be an object")
    return value


def _decode_value(claim_id: str, raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"claim {claim_id} has invalid value_json") from error


def backfill_conflict_keys_v2(connection: sqlite3.Connection) -> int:
    """在单事务中回填全部 v1 claim，并返回更新行数。"""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,)
    ).fetchone():
        return 0

    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
            "conflict_key,legacy_conflict_key,conflict_key_version FROM claims ORDER BY id"
        ).fetchall()
        updated = 0
        for row in rows:
            claim = dict(row)
            if claim["conflict_key_version"] == 2:
                continue
            qualifiers = _decode_qualifiers(claim["id"], claim["qualifiers_json"])
            value = _decode_value(claim["id"], claim["value_json"])
            namespace = str(claim["namespace_key"] or "default")
            subject = str(claim["subject_entity_id"] or "")
            predicate = str(claim["predicate"] or "")
            attribute = infer_canonical_attribute(predicate, subject, value, qualifiers)
            legacy_key = claim["legacy_conflict_key"] or claim["conflict_key"] or compute_legacy_conflict_key(
                namespace, subject, predicate, qualifiers
            )
            v2_key = compute_conflict_key(namespace, subject, attribute, qualifiers)
            connection.execute(
                "UPDATE claims SET canonical_attribute=?,conflict_key_version=2,"
                "legacy_conflict_key=?,conflict_key=? WHERE id=? AND conflict_key_version=1",
                (attribute, legacy_key, v2_key, claim["id"]),
            )
            updated += 1
        connection.execute(
            "INSERT INTO schema_migrations(version) VALUES (?)", (DATA_MIGRATION_VERSION,)
        )
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
