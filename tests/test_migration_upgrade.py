"""验证 v0.6 历史数据库可完整升级到当前 schema。"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[1] / "src/hl_mem/storage/migrations"
V010_FIXTURE = Path(__file__).resolve().parent / "fixtures/v010_after_018.sql"


def test_v006_database_upgrades_to_current_schema(tmp_path: Path) -> None:
    """001–006 数据库升级后应包含全部 migration，且保留基本读写能力。"""
    database_path = tmp_path / "v006.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    historical_migrations = sorted(MIGRATION_DIR.glob("00[1-6]_*.sql"))
    assert len(historical_migrations) == 6
    for migration in historical_migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    connection.commit()
    connection.close()

    database = Database(database_path)
    try:
        upgraded = database.open()
        expected_versions = {migration.stem for migration in MIGRATION_DIR.glob("*.sql")}
        applied_versions = {row[0] for row in upgraded.execute("SELECT version FROM schema_migrations")}
        assert expected_versions <= applied_versions
        assert "035_retrieval_feedback_injected" in applied_versions
        feedback_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(retrieval_feedback)")}
        assert "injected" in feedback_columns
        assert "used_by_model" not in feedback_columns

        upgraded.execute(
            "INSERT INTO events "
            "(id, event_type, actor_type, content_json, occurred_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "upgrade-event",
                "message",
                "user",
                '{"text":"migration ok"}',
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:00:00Z",
            ),
        )
        upgraded.commit()
        row = upgraded.execute("SELECT content_json FROM events WHERE id=?", ("upgrade-event",)).fetchone()
        assert row is not None
        assert row[0] == '{"text":"migration ok"}'
    finally:
        database.close()


def test_v010_snapshot_preserves_data_through_current_schema(tmp_path: Path) -> None:
    """018 历史快照升级后保留关系、FTS、向量与双时间字段。"""
    database_path = tmp_path / "v010.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    historical_migrations = sorted(MIGRATION_DIR.glob("*.sql"))[:18]
    assert historical_migrations[-1].stem.startswith("018_")
    for migration in historical_migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    connection.executescript(V010_FIXTURE.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO retrieval_feedback("
        "id,query_id,memory_type,memory_id,used_by_model,helpful,task_outcome,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            "feedback-before-rename",
            "query-before-rename",
            "claim",
            "claim-018-1",
            1,
            None,
            None,
            "2026-07-26T00:00:00Z",
        ),
    )
    expected_counts = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("events", "claims", "evidence_links", "episodes", "traces")
    }
    connection.commit()
    connection.close()

    database = Database(database_path)
    try:
        upgraded = database.open()
        for table, expected_count in expected_counts.items():
            assert upgraded.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == expected_count
        assert (
            upgraded.execute(
                "SELECT count(*) FROM claims_fts WHERE claims_fts MATCH ?",
                ('"历史迁移保留测试"',),
            ).fetchone()[0]
            == 1
        )
        blob = upgraded.execute("SELECT embedding_dense FROM claims WHERE id='claim-018-1'").fetchone()[0]
        assert struct.unpack("<3f", blob) == (1.0, 2.0, 3.0)
        temporal = upgraded.execute(
            "SELECT valid_from,valid_to,recorded_from,recorded_to FROM claims WHERE id='claim-018-2'"
        ).fetchone()
        assert tuple(temporal) == (
            "2025-01-02T00:00:00Z",
            "2025-12-31T23:59:59Z",
            "2025-01-02T00:00:01Z",
            None,
        )
        expected_versions = {migration.stem for migration in MIGRATION_DIR.glob("*.sql")}
        applied_versions = {row[0] for row in upgraded.execute("SELECT version FROM schema_migrations")}
        assert expected_versions <= applied_versions
        assert "035_retrieval_feedback_injected" in applied_versions
        feedback_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(retrieval_feedback)")}
        assert "injected" in feedback_columns
        assert "used_by_model" not in feedback_columns
        renamed_feedback = upgraded.execute(
            "SELECT injected,helpful,task_outcome FROM retrieval_feedback WHERE id='feedback-before-rename'"
        ).fetchone()
        assert tuple(renamed_feedback) == (1, None, None)
    finally:
        database.close()
