"""验证 migration 036 创建 tokenized FTS v2 schema。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

V2_TABLES = {"claims_fts_v2", "events_fts_v2", "claims_tags_fts_v2"}
LEGACY_TABLES = {"claims_fts", "events_fts", "claims_tags_fts"}


@pytest.fixture
def migrated_database(tmp_path: Path) -> Iterator[tuple[Database, object]]:
    database = Database(tmp_path / "migration-036.db")
    connection = database.open()
    yield database, connection
    database.close()


def test_migration_036_creates_unicode61_v2_tables_and_keeps_legacy_tables(
    migrated_database: tuple[Database, object],
) -> None:
    """缺少任一 v2 表、错误 tokenizer 或删除旧表都必须失败。"""
    _, connection = migrated_database
    schemas = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name IN ('claims_fts_v2','events_fts_v2','claims_tags_fts_v2')"
        )
    }
    legacy_tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('claims_fts','events_fts','claims_tags_fts')"
        )
    }

    assert set(schemas) == V2_TABLES
    assert all("tokenize='unicode61'" in schema for schema in schemas.values())
    assert legacy_tables == LEGACY_TABLES
    assert (
        connection.execute("SELECT version FROM schema_migrations WHERE version='036_tokenized_fts_v2'").fetchone()[
            "version"
        ]
        == "036_tokenized_fts_v2"
    )


def test_claim_delete_trigger_removes_both_v2_index_rows(
    migrated_database: tuple[Database, object],
) -> None:
    """删除 claim 时不得在 terms 或 tags v2 索引留下孤儿行。"""
    _, connection = migrated_database
    cursor = connection.execute(
        "INSERT INTO claims(id,recorded_from,status,index_text,topic_tags_json) VALUES(?,?,?,?,?)",
        ("claim-1", "2026-08-01T00:00:00+00:00", "active", "记忆 系统", '["memory"]'),
    )
    rowid = cursor.lastrowid
    connection.execute("INSERT INTO claims_fts_v2(rowid,terms) VALUES(?,?)", (rowid, "记忆 系统"))
    connection.execute("INSERT INTO claims_tags_fts_v2(rowid,tags_text) VALUES(?,?)", (rowid, "memory"))

    connection.execute("DELETE FROM claims WHERE rowid=?", (rowid,))

    assert connection.execute("SELECT 1 FROM claims_fts_v2 WHERE rowid=?", (rowid,)).fetchone() is None
    assert connection.execute("SELECT 1 FROM claims_tags_fts_v2 WHERE rowid=?", (rowid,)).fetchone() is None


def test_event_delete_trigger_removes_v2_index_row(
    migrated_database: tuple[Database, object],
) -> None:
    """删除 event 时不得在 events v2 索引留下孤儿行。"""
    _, connection = migrated_database
    cursor = connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) " "VALUES(?,?,?,?,?,?)",
        (
            "event-1",
            "message",
            "user",
            '{"text":"记忆系统"}',
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    rowid = cursor.lastrowid
    connection.execute("INSERT INTO events_fts_v2(rowid,terms) VALUES(?,?)", (rowid, "记忆 系统"))

    connection.execute("DELETE FROM events WHERE rowid=?", (rowid,))

    assert connection.execute("SELECT 1 FROM events_fts_v2 WHERE rowid=?", (rowid,)).fetchone() is None


def test_database_startup_atomically_backfills_existing_source_rows(tmp_path: Path) -> None:
    """升级后的首次 open 必须在 v2-only 查询暴露前补齐三个索引。"""
    database_path = tmp_path / "existing-pre-v2-data.db"
    initial_database = Database(database_path)
    initial_database.open()
    initial_database.close()

    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO claims(id,recorded_from,status,index_text,topic_tags_json) VALUES(?,?,?,?,?)",
        (
            "claim-existing",
            "2026-08-01T00:00:00+00:00",
            "active",
            "提取任务使用 qwen3.7-plus 模型",
            '["config.model"]',
        ),
    )
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) VALUES(?,?,?,?,?,?)",
        (
            "event-existing",
            "message",
            "user",
            '{"text":"中文事件使用 GPU 模型"}',
            "2026-08-01T00:00:00+00:00",
            "2026-08-01T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    upgraded_database = Database(database_path)
    upgraded_connection = upgraded_database.open()
    try:
        assert [claim["id"] for claim in ClaimRepository(upgraded_connection).search_claims_fts("提取 模型")] == [
            "claim-existing"
        ]
        assert [claim["id"] for claim in ClaimRepository(upgraded_connection).search_claims_tags(["config.model"])] == [
            "claim-existing"
        ]
        assert [event["id"] for event in EventRepository(upgraded_connection).search_events_fts("中文 GPU")] == [
            "event-existing"
        ]
    finally:
        upgraded_database.close()
