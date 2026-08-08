"""历史 claim persona subject 归一化迁移测试。"""

from __future__ import annotations

import json
import logging
import struct

from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.storage.database import Database
from hl_mem.storage.migrations.backfill_subject_canonicalization import (
    DATA_MIGRATION_VERSION,
    LEGACY_DATA_MIGRATION_VERSION,
    backfill_subject_canonicalization,
)
from hl_mem.storage.migrations.fact_hash_v2 import compute_fact_hash_v2


def _insert_claim(
    connection,
    claim_id: str,
    namespace: str,
    subject: str,
    index_text: str,
    *,
    value: str = "深色模式",
    entities: list[str] | None = None,
    fact_hash: str = "stale-fact-hash",
    conflict_key: str = "stale-conflict-key",
    embedding: bytes | None = None,
) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
        "canonical_slot,topic_tags_json,fact_hash,conflict_key,conflict_key_version,"
        "index_text,recorded_from,status,entities_json,embedding_dense,embedding_sparse,"
        "embedding_model,embedding_dim"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            namespace,
            subject,
            "偏好",
            json.dumps(value, ensure_ascii=False),
            "{}",
            "preference.ui_theme",
            "[]",
            fact_hash,
            conflict_key,
            3,
            index_text,
            "2026-08-01T00:00:00+00:00",
            "active",
            json.dumps(entities, ensure_ascii=False) if entities is not None else None,
            embedding,
            b"sparse" if embedding is not None else None,
            "legacy-model" if embedding is not None else None,
            2 if embedding is not None else None,
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


def test_backfill_invalidates_embeddings_updates_entities_and_reports_active_collisions(
    tmp_path,
    caplog,
) -> None:
    connection = Database(tmp_path / "subject-backfill-derived.db").open()
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,))
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)",
        (LEGACY_DATA_MIGRATION_VERSION,),
    )
    connection.execute(
        "INSERT INTO vector_index_state("
        "backend,enabled,schema_version,build_status,embedding_model,embedding_dim"
        ") VALUES('sqlite_vec',1,1,'ready','legacy-model',2)"
    )
    embedding = struct.pack("<ff", 1.0, 0.0)
    exact_hash = compute_fact_hash_v2("user", "偏好", "深色模式")
    dark_conflict_key = compute_conflict_key(
        "tenant-a",
        "user",
        "偏好",
        "preference.ui_theme",
        {},
    )
    _insert_claim(
        connection,
        "persona-affected",
        "tenant-a",
        "我",
        "我：深色模式",
        entities=["我", "Alice", "ＵＳＥＲ"],
        embedding=embedding,
    )
    _insert_claim(
        connection,
        "canonical-duplicate",
        "tenant-a",
        "user",
        "user：深色模式",
        fact_hash=exact_hash,
        conflict_key=dark_conflict_key,
    )
    _insert_claim(
        connection,
        "canonical-conflict",
        "tenant-a",
        "user",
        "user：浅色模式",
        value="浅色模式",
        fact_hash=compute_fact_hash_v2("user", "偏好", "浅色模式"),
        conflict_key=dark_conflict_key,
    )
    connection.commit()

    with caplog.at_level(logging.WARNING):
        assert backfill_subject_canonicalization(connection) == 3

    row = connection.execute(
        "SELECT subject_entity_id,entities_json,embedding_dense,embedding_sparse,"
        "embedding_model,embedding_dim FROM claims WHERE id='persona-affected'"
    ).fetchone()
    assert row["subject_entity_id"] == "user"
    assert json.loads(row["entities_json"]) == ["user", "Alice", "user"]
    assert tuple(row[key] for key in ("embedding_dense", "embedding_sparse", "embedding_model", "embedding_dim")) == (
        None,
        None,
        None,
        None,
    )
    assert (
        connection.execute("SELECT reason FROM claim_vector_dirty WHERE claim_id='persona-affected'").fetchone()[0]
        == "subject_canonicalization"
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM claims WHERE namespace_key='tenant-a' AND status='active'").fetchone()[
            0
        ]
        == 3
    )
    report = next(record.getMessage() for record in caplog.records if "subject canonicalization" in record.getMessage())
    assert "duplicate_groups=1" in report
    assert "duplicate_rows=1" in report
    assert "conflict_groups=1" in report
    assert "conflict_rows=3" in report
