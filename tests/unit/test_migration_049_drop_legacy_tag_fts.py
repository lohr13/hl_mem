from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"
MIGRATION_049 = MIGRATION_DIR / "049_drop_legacy_claims_tags_fts.sql"
LEGACY_OBJECTS = {
    "claims_tags_fts",
    "claims_tags_ai",
    "claims_tags_ad",
    "claims_tags_au",
}


def _pre_049_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "049_"]
    assert migrations[-1].name == "048_dedup_pair_injection_signals.sql"
    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    for data_version in (
        "006_data_conflict_key_v2",
        "011_data_fact_hash_v2",
        "016_data_conflict_key_v3",
        "038_data_subject_canonicalization_v2",
    ):
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (data_version,))
    connection.commit()
    return connection


def _legacy_objects(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name IN (?,?,?,?)",
            tuple(sorted(LEGACY_OBJECTS)),
        ).fetchall()
    }


def test_migration_049_drops_only_legacy_tag_fts_after_version_gate(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    legacy = _pre_049_database(path)
    legacy.execute(
        "INSERT INTO claims(id,value_json,topic_tags_json,recorded_from,status) "
        "VALUES ('claim','\"value\"','[\"architecture\"]','2026-08-18T00:00:00+00:00','active')"
    )
    legacy.commit()
    legacy.close()

    database = Database(path)
    connection = database.open()

    assert _legacy_objects(connection) == set()
    assert connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='claims_tags_fts_v2'").fetchone()
    assert connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='049_drop_legacy_claims_tags_fts'"
    ).fetchone()
    database.close()


@pytest.mark.parametrize(
    "consumer_sql",
    (
        "CREATE VIEW legacy_tag_consumer AS SELECT rowid FROM claims_tags_fts",
        "CREATE TRIGGER legacy_tag_consumer AFTER INSERT ON audit_log BEGIN "
        "SELECT count(*) FROM claims_tags_fts; END",
    ),
)
def test_migration_049_detects_internal_consumers_before_any_drop(
    tmp_path: Path,
    consumer_sql: str,
) -> None:
    path = tmp_path / "consumer.db"
    legacy = _pre_049_database(path)
    legacy.execute(consumer_sql)
    legacy.commit()
    legacy.close()

    with pytest.raises(sqlite3.IntegrityError, match="database consumer"):
        Database(path).open()

    forensic = sqlite3.connect(path)
    try:
        assert _legacy_objects(forensic) == LEGACY_OBJECTS
        assert (
            forensic.execute(
                "SELECT 1 FROM schema_migrations WHERE version='049_drop_legacy_claims_tags_fts'"
            ).fetchone()
            is None
        )
        assert forensic.execute("SELECT 1 FROM sqlite_schema WHERE name='legacy_tag_consumer'").fetchone()
    finally:
        forensic.close()


def test_migration_049_fails_closed_without_runtime_floor_evidence(tmp_path: Path) -> None:
    path = tmp_path / "version-gate.db"
    legacy = _pre_049_database(path)
    legacy.execute("DELETE FROM schema_migrations WHERE version='048_dedup_pair_injection_signals'")
    legacy.commit()

    with pytest.raises(sqlite3.IntegrityError, match="runtime floor"):
        Database(path)._apply_sql_migration(legacy, MIGRATION_049)

    assert _legacy_objects(legacy) == LEGACY_OBJECTS
    assert (
        legacy.execute("SELECT 1 FROM schema_migrations WHERE version='049_drop_legacy_claims_tags_fts'").fetchone()
        is None
    )
    legacy.close()
