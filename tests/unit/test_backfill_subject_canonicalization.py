"""历史 claim persona subject 归一化迁移测试。"""

from __future__ import annotations

import json

from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.storage.database import Database
from hl_mem.storage.migrations.backfill_subject_canonicalization import (
    DATA_MIGRATION_VERSION,
    backfill_subject_canonicalization,
)
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2


def _insert_claim(connection, claim_id: str, namespace: str, subject: str, index_text: str) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
        "canonical_slot,topic_tags_json,fact_hash,conflict_key,conflict_key_version,"
        "index_text,recorded_from,status"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            namespace,
            subject,
            "偏好",
            json.dumps("深色模式", ensure_ascii=False),
            "{}",
            "preference.ui_theme",
            "[]",
            "stale-fact-hash",
            "stale-conflict-key",
            3,
            index_text,
            "2026-08-01T00:00:00+00:00",
            "active",
        ),
    )


def test_backfill_canonicalizes_only_persona_subjects_and_recomputes_keys(tmp_path) -> None:
    connection = Database(tmp_path / "subject-backfill.db").open()
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,))
    _insert_claim(connection, "persona-a", "tenant-a", "我", "我：深色模式")
    _insert_claim(connection, "persona-b", "tenant-b", "ＵＳＥＲ", "ＵＳＥＲ 偏好 深色模式")
    _insert_claim(connection, "named", "tenant-a", "Alice", "Alice：深色模式")
    connection.commit()

    assert backfill_subject_canonicalization(connection) == 2

    rows = {
        row["id"]: dict(row)
        for row in connection.execute(
            "SELECT id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
            "canonical_slot,fact_hash,conflict_key,index_text FROM claims ORDER BY id"
        )
    }
    expected_fact_hash = compute_fact_hash_v2("user", "偏好", "深色模式")
    assert rows["persona-a"]["subject_entity_id"] == "user"
    assert rows["persona-b"]["subject_entity_id"] == "user"
    assert rows["persona-a"]["fact_hash"] == rows["persona-b"]["fact_hash"] == expected_fact_hash
    assert rows["persona-a"]["conflict_key"] == compute_conflict_key(
        "tenant-a", "user", "偏好", "preference.ui_theme", {}
    )
    assert rows["persona-b"]["conflict_key"] == compute_conflict_key(
        "tenant-b", "user", "偏好", "preference.ui_theme", {}
    )
    assert rows["persona-a"]["conflict_key"] != rows["persona-b"]["conflict_key"]
    assert rows["persona-a"]["index_text"] == "user：深色模式"
    assert rows["persona-b"]["index_text"] == "user 偏好 深色模式"
    assert rows["named"]["subject_entity_id"] == "Alice"
    assert rows["named"]["fact_hash"] == "stale-fact-hash"
    assert rows["named"]["index_text"] == "Alice：深色模式"
    assert backfill_subject_canonicalization(connection) == 0


def test_database_open_registers_subject_backfill(tmp_path) -> None:
    connection = Database(tmp_path / "registered.db").open()

    versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}

    assert "038_subject_canonicalization" in versions
    assert DATA_MIGRATION_VERSION in versions
