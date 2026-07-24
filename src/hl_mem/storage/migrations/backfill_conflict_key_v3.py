"""把存量 Claim 的冲突键可恢复地升级为不含 predicate 的 v3。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hl_mem.domain.claims.conflicts import compute_conflict_key

DATA_MIGRATION_VERSION = "016_data_conflict_key_v3"


def _decode_qualifiers(claim_id: str, raw: Any) -> dict[str, Any]:
    """解码历史 qualifier，并拒绝损坏数据。"""
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError as error:
        raise ValueError(f"claim {claim_id} has invalid qualifiers_json") from error
    if not isinstance(value, dict):
        raise ValueError(f"claim {claim_id} qualifiers_json must be an object")
    return value


def backfill_conflict_keys_v3(connection: sqlite3.Connection) -> int:
    """只更新冲突键及其版本，并把旧键保存在 legacy_conflict_key。"""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?",
        (DATA_MIGRATION_VERSION,),
    ).fetchone():
        return 0

    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id,namespace_key,subject_entity_id,predicate,canonical_slot,"
            "qualifiers_json,conflict_key,legacy_conflict_key,conflict_key_version "
            "FROM claims WHERE conflict_key_version<3 ORDER BY id"
        ).fetchall()
        updated = 0
        for row in rows:
            claim = dict(row)
            qualifiers = _decode_qualifiers(str(claim["id"]), claim["qualifiers_json"])
            key = compute_conflict_key(
                str(claim["namespace_key"] or "default"),
                str(claim["subject_entity_id"] or ""),
                str(claim["predicate"] or ""),
                claim["canonical_slot"],
                qualifiers,
            )
            cursor = connection.execute(
                "UPDATE claims SET legacy_conflict_key=COALESCE(legacy_conflict_key,conflict_key),"
                "conflict_key=?,conflict_key_version=3 "
                "WHERE id=? AND conflict_key_version=? AND conflict_key IS ?",
                (key, claim["id"], claim["conflict_key_version"], claim["conflict_key"]),
            )
            updated += cursor.rowcount
        connection.execute(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            (DATA_MIGRATION_VERSION,),
        )
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
