from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hl_mem.storage.database import Database

NOW = "2026-08-15T08:00:00+00:00"
MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _insert_claim(connection: sqlite3.Connection, claim_id: str) -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,predicate,value_json,recorded_from,status) "
        "VALUES (?, 'default', 'knows', ?, ?, 'active')",
        (claim_id, f'"{claim_id}"', NOW),
    )


def test_migration_044_backfills_existing_relation_validity_from_created_at(tmp_path: Path) -> None:
    database_path = tmp_path / "pre-044.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "044_"]
    assert migrations[-1].name == "043_deletion_ledger_state.sql"
    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    _insert_claim(connection, "left")
    _insert_claim(connection, "right")
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('legacy','left','right','supports',?)",
        (NOW,),
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    try:
        upgraded = database.open()
        columns = {row[1] for row in upgraded.execute("PRAGMA table_info(memory_relations)")}
        relation = upgraded.execute("SELECT valid_from,valid_to FROM memory_relations WHERE id='legacy'").fetchone()

        assert {"valid_from", "valid_to"} <= columns
        assert tuple(relation) == (NOW, None)
        assert upgraded.execute("SELECT 1 FROM schema_migrations WHERE version='044_relation_bitemporal'").fetchone()
    finally:
        database.close()


def test_migration_044_defaults_new_relation_valid_from(tmp_path: Path) -> None:
    connection = Database(tmp_path / "new-edge.db").open()
    _insert_claim(connection, "left")
    _insert_claim(connection, "right")

    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('edge','left','right','supports',?)",
        (NOW,),
    )

    assert connection.execute("SELECT valid_from FROM memory_relations WHERE id='edge'").fetchone()[0] == NOW


@pytest.mark.parametrize("terminal_status", ("superseded", "expired"))
@pytest.mark.parametrize("endpoint", ("left", "right"))
def test_migration_044_terminal_transition_closes_open_relations_at_claim_valid_to(
    tmp_path: Path,
    terminal_status: str,
    endpoint: str,
) -> None:
    connection = Database(tmp_path / f"close-{terminal_status}-{endpoint}.db").open()
    _insert_claim(connection, "left")
    _insert_claim(connection, "right")
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('edge','left','right','supports',?)",
        (NOW,),
    )

    connection.execute(
        "UPDATE claims SET status=?,valid_to=? WHERE id=?",
        (terminal_status, NOW, endpoint),
    )

    assert connection.execute("SELECT valid_to FROM memory_relations WHERE id='edge'").fetchone()[0] == NOW


def test_migration_044_retraction_closes_edges_with_utc_timestamp(tmp_path: Path) -> None:
    connection = Database(tmp_path / "close-retracted.db").open()
    _insert_claim(connection, "left")
    _insert_claim(connection, "right")
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('edge','left','right','supports',?)",
        (NOW,),
    )
    before = datetime.now(timezone.utc)

    connection.execute("UPDATE claims SET status='retracted' WHERE id='left'")

    closed_at = connection.execute("SELECT valid_to FROM memory_relations WHERE id='edge'").fetchone()[0]
    parsed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    assert before - timedelta(seconds=1) <= parsed <= datetime.now(timezone.utc)


def test_migration_044_nonterminal_archive_does_not_close_relation(tmp_path: Path) -> None:
    connection = Database(tmp_path / "archive.db").open()
    _insert_claim(connection, "left")
    _insert_claim(connection, "right")
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('edge','left','right','supports',?)",
        (NOW,),
    )

    connection.execute("UPDATE claims SET status='archived' WHERE id='left'")

    assert connection.execute("SELECT valid_to FROM memory_relations WHERE id='edge'").fetchone()[0] is None
