from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _pre_047_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "047_"]
    assert migrations[-1].name == "046_operational_retention_indexes.sql"
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
    return connection


def test_migration_047_keeps_legacy_claims_observable_but_unknown(tmp_path: Path) -> None:
    path = tmp_path / "pre-047.db"
    legacy = _pre_047_database(path)
    legacy.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,?,?,'active')",
        ("legacy", '"legacy value"', "2026-08-18T00:00:00+00:00"),
    )
    legacy.commit()
    legacy.close()

    database = Database(path)
    connection = database.open()

    row = connection.execute("SELECT assertion_kind,status,valid_to FROM claims WHERE id='legacy'").fetchone()
    assert tuple(row) == ("unknown", "active", None)
    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='047_claim_assertion_kind'").fetchone()
    database.close()


def test_migration_047_rejects_invalid_assertion_kind(tmp_path: Path) -> None:
    connection = Database(tmp_path / "fresh.db").open()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "INSERT INTO claims(id,value_json,recorded_from,status,assertion_kind) " "VALUES (?,?,?,'active','level')",
            ("invalid", '"value"', "2026-08-18T00:00:00+00:00"),
        )
