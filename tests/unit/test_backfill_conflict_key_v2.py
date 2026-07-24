import json
import sqlite3
from pathlib import Path

import pytest

from hl_mem.domain.claims.conflicts import compute_legacy_conflict_key
from hl_mem.storage.database import Database
from hl_mem.storage.migrations.backfill_conflict_key_v2 import (
    DATA_MIGRATION_VERSION,
    backfill_conflict_keys_v2,
)


def _insert_v1_claim(connection, claim_id, predicate, value, conflict_key=None, qualifiers=None):
    connection.execute(
        "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
        "conflict_key,recorded_from,status) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            "default",
            "用户",
            predicate,
            json.dumps(value, ensure_ascii=False),
            json.dumps(qualifiers or {}, ensure_ascii=False),
            conflict_key or compute_legacy_conflict_key("default", "用户", predicate, qualifiers or {}),
            "2026-07-21T00:00:00+00:00",
            "active",
        ),
    )
    connection.commit()


def test_backfill_preserves_legacy_key_and_is_idempotent(tmp_path) -> None:
    connection = Database(tmp_path / "backfill.db").open()
    old_key = compute_legacy_conflict_key("default", "用户", "使用", {})
    _insert_v1_claim(connection, "claim-1", "使用", "PostgreSQL", old_key)
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,))
    connection.commit()

    assert backfill_conflict_keys_v2(connection) == 1
    first = dict(connection.execute("SELECT * FROM claims WHERE id='claim-1'").fetchone())
    assert first["canonical_attribute"] == "choice.database"
    assert first["conflict_key_version"] == 2
    assert first["legacy_conflict_key"] == old_key
    assert first["conflict_key"] != old_key
    assert backfill_conflict_keys_v2(connection) == 0
    assert dict(connection.execute("SELECT * FROM claims WHERE id='claim-1'").fetchone()) == first


def test_backfill_rolls_back_whole_batch_on_malformed_json(tmp_path) -> None:
    connection = Database(tmp_path / "rollback.db").open()
    _insert_v1_claim(connection, "good", "使用", "SQLite")
    _insert_v1_claim(connection, "bad", "配置", "端口 10808")
    connection.execute("UPDATE claims SET qualifiers_json='{' WHERE id='bad'")
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,))
    connection.commit()

    with pytest.raises(ValueError, match="bad"):
        backfill_conflict_keys_v2(connection)

    rows = connection.execute(
        "SELECT conflict_key_version,legacy_conflict_key FROM claims ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(1, None), (1, None)]
    assert connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,)
    ).fetchone() is None


def test_006_migration_stales_observations_and_runs_data_backfill(tmp_path) -> None:
    path = tmp_path / "upgrade.db"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    migration_dir = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"
    connection.execute(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for migration in sorted(migration_dir.glob("00[1-5]_*.sql")):
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    connection.execute(
        "INSERT INTO derivations(id,kind,body,status,updated_at) "
        "VALUES ('obs','observation','旧归纳','active','2026-07-21T00:00:00+00:00')"
    )
    _insert_v1_claim(connection, "legacy", "配置", "端口 10808")
    connection.close()

    upgraded = Database(path).open()
    claim = upgraded.execute(
        "SELECT canonical_attribute,conflict_key_version,legacy_conflict_key FROM claims WHERE id='legacy'"
    ).fetchone()
    assert tuple(claim) == ("config.port", 2, compute_legacy_conflict_key("default", "用户", "配置", {}))
    assert upgraded.execute("SELECT status FROM derivations WHERE id='obs'").fetchone()[0] == "stale"
    versions = {row[0] for row in upgraded.execute("SELECT version FROM schema_migrations")}
    assert {"006_canonical_attribute", DATA_MIGRATION_VERSION} <= versions
