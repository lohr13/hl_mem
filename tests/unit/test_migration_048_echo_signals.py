from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _pre_048_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "048_"]
    assert migrations[-1].name == "047_claim_assertion_kind.sql"
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


def test_migration_048_marks_legacy_rows_without_guessing_new_endpoint(tmp_path) -> None:
    path = tmp_path / "upgrade.db"
    legacy = _pre_048_database(path)
    legacy.executemany(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,'\"x\"','2026-01-01','active')",
        (("left",), ("right",)),
    )
    legacy.execute(
        "INSERT INTO dedup_pairs(id,pair_key,left_claim_id,right_claim_id,similarity,created_at) "
        "VALUES ('pair','left:right','left','right',0.96,'2026-01-01')"
    )
    legacy.commit()
    legacy.close()

    upgraded = Database(path).open()
    row = upgraded.execute("SELECT pair_source,new_claim_id FROM dedup_pairs WHERE id='pair'").fetchone()
    indexes = {item["name"] for item in upgraded.execute("PRAGMA index_list('dedup_pairs')")}

    assert tuple(row) == ("legacy", None)
    assert "idx_dedup_pairs_pending_new_claim" in indexes
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute("UPDATE dedup_pairs SET pair_source='guessed' WHERE id='pair'")
