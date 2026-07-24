"""验证 migration 022 从旧版 FTS schema 升级到 trigram。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"
TRIGGER_NAMES = {
    "claims_ai",
    "claims_ad",
    "claims_au",
    "claims_tags_ai",
    "claims_tags_ad",
    "claims_tags_au",
}


def _create_v021_database(path: Path) -> None:
    """执行 001-021 migration，并写入供 022 回填的旧数据。"""
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for migration in sorted(MIGRATION_DIR.glob("*.sql")):
        if int(migration.stem.split("_", maxsplit=1)[0]) > 21:
            continue
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    for claim_id, text in (
        ("claim-memory", "记忆系统架构设计"),
        ("claim-sqlite", "SQLite FTS5 trigram"),
        ("claim-codex", "使用 Codex CLI 辅助开发"),
    ):
        connection.execute(
            "INSERT INTO claims(id, predicate, value_json, topic_tags_json, recorded_from, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                claim_id,
                "描述",
                json.dumps(text, ensure_ascii=False),
                json.dumps(["architecture"], ensure_ascii=False),
                "2026-07-24T00:00:00+00:00",
                "active",
            ),
        )
    connection.commit()
    connection.close()


@pytest.fixture
def upgraded_database(tmp_path: Path) -> Iterator[tuple[Database, sqlite3.Connection]]:
    """构造含旧数据的 v0.10 schema，并通过 Database 自动升级。"""
    path = tmp_path / "migration-022.db"
    _create_v021_database(path)
    database = Database(path)
    connection = database.open()
    yield database, connection
    database.close()


def test_migration_022_upgrades_fts_to_trigram(
    upgraded_database: tuple[Database, sqlite3.Connection],
) -> None:
    """升级后 claims 与 tags FTS 均使用 trigram 且旧数据已回填。"""
    _, connection = upgraded_database
    schemas = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('claims_fts', 'claims_tags_fts')"
        )
    }
    assert set(schemas) == {"claims_fts", "claims_tags_fts"}
    assert all("tokenize='trigram'" in sql for sql in schemas.values())
    assert connection.execute(
        "SELECT count(*) FROM claims_fts WHERE claims_fts MATCH '\"记忆系统\"'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM claims_tags_fts WHERE claims_tags_fts MATCH '\"architecture\"'"
    ).fetchone()[0] == 3


def test_chinese_fts_returns_results_after_migration(
    upgraded_database: tuple[Database, sqlite3.Connection],
) -> None:
    """升级后中文连续子串能命中迁移前写入的 claim。"""
    _, connection = upgraded_database
    results = ClaimRepository(connection).search_claims_fts("记忆系统")
    assert [result["id"] for result in results] == ["claim-memory"]


def test_triggers_exist_after_migration(
    upgraded_database: tuple[Database, sqlite3.Connection],
) -> None:
    """升级后两个 FTS 表所需的六个同步触发器完整存在。"""
    _, connection = upgraded_database
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?, ?, ?, ?, ?, ?)",
        tuple(sorted(TRIGGER_NAMES)),
    )
    assert {row["name"] for row in rows} == TRIGGER_NAMES


def test_schema_migrations_registered(
    upgraded_database: tuple[Database, sqlite3.Connection],
) -> None:
    """升级后 schema_migrations 记录 migration 022。"""
    _, connection = upgraded_database
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE version='022_fts_trigram'"
    ).fetchone()
    assert row["version"] == "022_fts_trigram"


def test_events_fts_still_unicode61(
    upgraded_database: tuple[Database, sqlite3.Connection],
) -> None:
    """migration 022 不改变 events FTS 的 unicode61 中文整词行为。"""
    _, connection = upgraded_database
    repository = EventRepository(connection)
    assert repository.insert_event(
        {
            "id": "event-memory",
            "event_type": "message",
            "actor_type": "user",
            "content": {"text": "这是记忆系统架构设计"},
            "occurred_at": "2026-07-24T00:00:00+00:00",
            "recorded_at": "2026-07-24T00:00:00+00:00",
        }
    )
    assert repository.search_events_fts("记忆系统") == []
