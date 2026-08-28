from __future__ import annotations

import sqlite3

import pytest

from hl_mem.application.conflicts import ResolutionService
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.repair_active_claims import repair_active_claims
from tests.unit._conflict_fixture import seed_pre_041_history

NOW = "2026-08-15T08:00:00+00:00"


def _claim(
    repository: ClaimRepository,
    claim_id: str,
    *,
    value: str,
    status: str,
    slot: str | None = "config.port",
    conflict_key: str | None = "port-group",
    fact_hash: str | None = None,
) -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "gateway",
            "predicate": "配置",
            "value": value,
            "qualifiers": {"service": "gateway"} if slot == "config.port" else {},
            "canonical_attribute": slot or "fact.other",
            "canonical_slot": slot,
            "fact_hash": fact_hash or f"hash-{claim_id}",
            "conflict_key": conflict_key,
            "conflict_key_version": 3,
            "valid_from": NOW,
            "recorded_from": NOW,
            "observed_at": NOW,
            "status": status,
            "confidence": 0.9,
            "importance": 0.5,
            "scope": "permanent",
            "volatility": "stable",
            "source_authority": "medium",
        }
    )


def test_migration_041_blocks_direct_sql_second_active_insert(tmp_path) -> None:
    connection = Database(tmp_path / "guard-insert.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "first", value="8080", status="active")

    with pytest.raises(sqlite3.IntegrityError, match="exclusive conflict group already has an active claim"):
        connection.execute(
            "INSERT INTO claims("
            "id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,canonical_attribute,"
            "canonical_slot,fact_hash,conflict_key,conflict_key_version,recorded_from,status"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "second",
                "default",
                "gateway",
                "配置",
                '"8081"',
                '{"service":"gateway"}',
                "config.port",
                "config.port",
                "hash-second",
                "port-group",
                3,
                NOW,
                "active",
            ),
        )
    connection.rollback()

    assert (
        connection.execute(
            "SELECT count(*) FROM claims WHERE conflict_key='port-group' AND status='active'"
        ).fetchone()[0]
        == 1
    )


def test_migration_041_blocks_direct_sql_activation_update(tmp_path) -> None:
    connection = Database(tmp_path / "guard-update.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "first", value="8080", status="active")
    _claim(repository, "second", value="8081", status="candidate")

    with pytest.raises(sqlite3.IntegrityError, match="exclusive conflict group already has an active claim"):
        connection.execute("UPDATE claims SET status='active' WHERE id='second'")
    connection.rollback()

    assert repository.get_claim("second")["status"] == "candidate"


def test_migration_041_allows_non_group_update_on_historical_dirty_group(tmp_path) -> None:
    connection = Database(tmp_path / "guard-history.db").open()
    repository = ClaimRepository(connection)
    with seed_pre_041_history(connection):
        _claim(repository, "first", value="8080", status="active")
        _claim(repository, "second", value="8081", status="active")

    cursor = connection.execute("UPDATE claims SET access_count=access_count+1 WHERE id='first'")

    assert cursor.rowcount == 1
    assert repository.get_claim("first")["access_count"] == 1


def test_migration_041_allows_resolution_and_repair_legal_paths(tmp_path) -> None:
    connection = Database(tmp_path / "guard-legal.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", value="8080", status="disputed")
    _claim(repository, "right", value="8081", status="disputed")
    assert repository.insert_conflict_case(
        {
            "id": "case",
            "pair_key": "left-right",
            "left_claim_id": "left",
            "right_claim_id": "right",
            "status": "manual_required",
            "created_at": NOW,
        }
    )

    ResolutionService(connection).resolve("case", "keep_left", resolved_at=NOW)

    assert repository.get_claim("left")["status"] == "active"
    assert repository.get_claim("right")["status"] == "superseded"

    _claim(
        repository,
        "exact-a",
        value="same",
        status="active",
        slot=None,
        conflict_key=None,
        fact_hash="same-hash",
    )
    _claim(
        repository,
        "exact-b",
        value="same",
        status="active",
        slot=None,
        conflict_key=None,
        fact_hash="same-hash",
    )

    repaired = repair_active_claims(connection, apply=True, repaired_at=NOW)

    assert repaired["after"]["healthy"] is True
    assert (
        connection.execute("SELECT count(*) FROM claims WHERE fact_hash='same-hash' AND status='active'").fetchone()[0]
        == 1
    )


def test_migration_041_registers_both_guard_triggers(tmp_path) -> None:
    connection = Database(tmp_path / "guard-schema.db").open()

    names = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'claims_active_exclusive_guard_%'"
        )
    }

    assert names == {"claims_active_exclusive_guard_insert", "claims_active_exclusive_guard_update"}
    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='041_active_claim_guard'").fetchone()


def test_migration_041_repairs_missing_registered_trigger(tmp_path) -> None:
    path = tmp_path / "guard-repair.db"
    database = Database(path)
    connection = database.open()
    connection.execute("DROP TRIGGER claims_active_exclusive_guard_update")
    connection.commit()
    database.close()

    repaired = Database(path).open()
    names = {
        row["name"]
        for row in repaired.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'claims_active_exclusive_guard_%'"
        )
    }

    assert names == {"claims_active_exclusive_guard_insert", "claims_active_exclusive_guard_update"}


def test_migration_041_repairs_registered_trigger_with_stale_definition(tmp_path) -> None:
    path = tmp_path / "guard-definition-repair.db"
    database = Database(path)
    connection = database.open()
    connection.execute("DROP TRIGGER claims_active_exclusive_guard_update")
    connection.execute(
        "CREATE TRIGGER claims_active_exclusive_guard_update BEFORE UPDATE ON claims "
        "WHEN NEW.status='active' BEGIN SELECT RAISE(ABORT,'stale guard'); END"
    )
    connection.commit()
    database.close()

    repaired = Database(path).open()
    trigger_sql = repaired.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='claims_active_exclusive_guard_update'"
    ).fetchone()[0]
    repaired.execute("INSERT INTO claims(id,status,recorded_from) VALUES('claim','active','2026-08-15T00:00:00+00:00')")
    cursor = repaired.execute("UPDATE claims SET access_count=access_count+1 WHERE id='claim'")

    assert "BEFORE UPDATE OF status, namespace_key, conflict_key, canonical_slot ON claims" in trigger_sql
    assert cursor.rowcount == 1
