from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database, register_entity_sqlite_functions

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def test_060_adds_only_two_closed_event_provenance_columns(tmp_path: Path) -> None:
    path = tmp_path / "pre-060.db"
    connection = sqlite3.connect(path)
    register_entity_sqlite_functions(connection)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "060_"]
    assert migrations[-1].name == "059_memory_relation_provenance.sql"
    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES ('legacy','message','user','{}','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')"
    )
    schema_objects_before = {
        tuple(row)
        for row in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
        )
    }
    event_columns_before = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    connection.commit()
    connection.close()

    database = Database(path)
    upgraded = database.open()
    columns = {row[1]: row for row in upgraded.execute("PRAGMA table_info(events)")}
    schema_objects_after = {
        tuple(row)
        for row in upgraded.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
        )
    }

    assert set(columns) - event_columns_before == {"origin_class", "session_kind"}
    assert columns["origin_class"][3:] == (1, "'unknown'", 0)
    assert columns["session_kind"][3:] == (1, "'unknown'", 0)
    assert tuple(upgraded.execute("SELECT origin_class,session_kind FROM events WHERE id='legacy'").fetchone()) == (
        "unknown",
        "unknown",
    )
    assert schema_objects_after == schema_objects_before
    assert upgraded.execute("SELECT 1 FROM schema_migrations WHERE version='060_event_provenance'").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(
            "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at,origin_class) "
            "VALUES ('bad','message','user','{}','2026-09-01','2026-09-01','invented')"
        )
    database.close()


def test_new_database_has_event_provenance_defaults(tmp_path: Path) -> None:
    database = Database(tmp_path / "new.db")
    connection = database.open()
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES ('new','message','user','{}','2026-09-01','2026-09-01')"
    )

    assert tuple(connection.execute("SELECT origin_class,session_kind FROM events").fetchone()) == (
        "unknown",
        "unknown",
    )
    database.close()
