"""fact_hash v2 算法与历史数据回填。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from typing import Any


DATA_MIGRATION_VERSION = "011_data_fact_hash_v2"


def compute_fact_hash_v2(subject: str, predicate: str, value: Any) -> str:
    """使用带类型边界的稳定 JSON 数组计算事实哈希。"""
    stable_value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = json.dumps(
        [
            "fact-v2",
            unicodedata.normalize("NFKC", subject).strip().casefold(),
            unicodedata.normalize("NFKC", predicate).strip().casefold(),
            stable_value,
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def backfill_fact_hash_v2(connection: sqlite3.Connection) -> int:
    """在单一事务内回填全部 claim 的 v2 fact_hash。"""
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,)
    ).fetchone():
        return 0

    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id,subject_entity_id,predicate,value_json FROM claims ORDER BY id"
        ).fetchall()
        updates: list[tuple[str, str]] = []
        for row in rows:
            raw_value = row["value_json"]
            if isinstance(raw_value, str):
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError as error:
                    raise ValueError(f"claim {row['id']} has invalid value_json") from error
            else:
                value = raw_value
            fact_hash = compute_fact_hash_v2(
                str(row["subject_entity_id"] or ""), str(row["predicate"] or ""), value
            )
            updates.append((fact_hash, row["id"]))
        connection.executemany("UPDATE claims SET fact_hash=? WHERE id=?", updates)
        connection.execute(
            "INSERT INTO schema_migrations(version) VALUES (?)", (DATA_MIGRATION_VERSION,)
        )
        connection.commit()
        return len(updates)
    except Exception:
        connection.rollback()
        raise
