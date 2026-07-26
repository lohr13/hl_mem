"""验证 v0.6 历史数据库可完整升级到当前 schema。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[1] / "src/hl_mem/storage/migrations"


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
        assert "029_ttl_scan_indexes" in applied_versions

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
