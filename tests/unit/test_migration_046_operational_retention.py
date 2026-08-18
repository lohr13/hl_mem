from __future__ import annotations

import sqlite3
from pathlib import Path

from hl_mem.storage.database import Database

NOW = "2026-08-18T08:00:00+00:00"
MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _pre_046_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "046_"]
    assert migrations[-1].name == "045_conflict_review_queue.sql"
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


def _insert_claims(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,?,?,'active')",
        (("left", '"left"', NOW), ("right", '"right"', NOW), ("third", '"third"', NOW)),
    )


def test_migration_046_fresh_schema_has_retention_indexes_and_terminal_decision(tmp_path: Path) -> None:
    connection = Database(tmp_path / "fresh.db").open()

    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_audit_cleanup",
        "idx_jobs_retention",
        "idx_dedup_pairs_retention",
        "idx_retrieval_feedback_retention",
    } <= indexes
    _insert_claims(connection)
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,decision,reviewed_at,created_at"
        ") VALUES ('dismissed','dismissed-pair','left','right',0.8,'dismissed_below_floor',?,?)",
        (NOW, NOW),
    )
    assert connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='046_operational_retention_indexes'"
    ).fetchone()


def test_migration_046_upgrades_below_floor_pending_pairs_once(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    legacy = _pre_046_database(path)
    _insert_claims(legacy)
    legacy.executemany(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,created_at"
        ") VALUES (?,?,?,?,?,?)",
        (
            ("below", "below-pair", "left", "right", 0.87, "2026-01-01T00:00:00+00:00"),
            ("current", "current-pair", "left", "third", 0.92, "2026-01-01T00:00:00+00:00"),
        ),
    )
    legacy.commit()
    legacy.close()

    database = Database(path)
    upgraded = database.open()
    first = [
        tuple(row)
        for row in upgraded.execute("SELECT id,decision,judge_reason,reviewed_at FROM dedup_pairs ORDER BY id")
    ]
    database.close()

    reopened_database = Database(path)
    reopened = reopened_database.open()
    second = [
        tuple(row)
        for row in reopened.execute("SELECT id,decision,judge_reason,reviewed_at FROM dedup_pairs ORDER BY id")
    ]
    reopened_database.close()

    assert first == second
    assert first[0][0:3] == ("below", "dismissed_below_floor", "v0.28.9_below_current_floor")
    assert first[0][3] is not None
    assert first[1] == ("current", None, None, None)
