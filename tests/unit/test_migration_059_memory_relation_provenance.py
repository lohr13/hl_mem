from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database, register_entity_sqlite_functions

NOW = "2026-08-31T00:00:00+00:00"
MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _insert_claim(connection: sqlite3.Connection, claim_id: str) -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,predicate,value_json,recorded_from,status) "
        "VALUES (?, 'default', 'knows', ?, ?, 'active')",
        (claim_id, f'"{claim_id}"', NOW),
    )


def test_059_backfills_legacy_relation_provenance_and_enforces_proposal_shape(tmp_path: Path) -> None:
    path = tmp_path / "pre-059.db"
    connection = sqlite3.connect(path)
    register_entity_sqlite_functions(connection)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "059_"]
    assert migrations[-1].name == "058_disable_v1_semantic_jobs.sql"
    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    _insert_claim(connection, "left")
    _insert_claim(connection, "right")
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('legacy-edge','left','right','supports',?)",
        (NOW,),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    upgraded = database.open()

    columns = {row[1] for row in upgraded.execute("PRAGMA table_info(memory_relations)")}
    assert {"provenance", "proposal_id"} <= columns
    assert tuple(
        upgraded.execute("SELECT provenance,proposal_id FROM memory_relations WHERE id='legacy-edge'").fetchone()
    ) == ("legacy", None)
    with pytest.raises(sqlite3.IntegrityError, match="approved_proposal requires matching pending proposal"):
        upgraded.execute(
            "INSERT INTO memory_relations("
            "id,from_id,to_id,relation,created_at,provenance,proposal_id"
            ") VALUES ('invalid-edge','left','right','supports',?,'approved_proposal',NULL)",
            (NOW,),
        )
    assert upgraded.execute("SELECT 1 FROM schema_migrations WHERE version='059_memory_relation_provenance'").fetchone()
    database.close()
