from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database

NOW = "2026-08-18T00:00:00+00:00"
MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _insert_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    value: str,
    slot: str = "config.port",
    conflict_key: str = "service-port",
    namespace: str = "default",
    status: str = "disputed",
) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,predicate,value_json,recorded_from,status,source_authority,"
        "canonical_attribute,canonical_slot,conflict_key"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            namespace,
            "配置",
            f'"{value}"',
            NOW,
            status,
            "medium",
            slot,
            slot,
            conflict_key,
        ),
    )


def _insert_case(
    connection: sqlite3.Connection,
    case_id: str,
    left_id: str,
    right_id: str,
    *,
    status: str = "manual_required",
    namespace: str | None = None,
    group_key: str | None = None,
) -> None:
    columns = "id,pair_key,left_claim_id,right_claim_id,status,created_at"
    values: tuple[object, ...] = (case_id, f"pair-{case_id}", left_id, right_id, status, NOW)
    if namespace is not None or group_key is not None:
        columns += ",namespace_key,group_key"
        values += (namespace, group_key)
    connection.execute(
        f"INSERT INTO conflict_cases({columns}) VALUES ({','.join('?' for _ in values)})",
        values,
    )


def _pre_045_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "045_"]
    assert migrations[-1].name == "044_relation_bitemporal.sql"
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


def test_migration_045_creates_group_candidate_and_review_schema(tmp_path: Path) -> None:
    connection = Database(tmp_path / "fresh.db").open()

    case_columns = {row[1] for row in connection.execute("PRAGMA table_info(conflict_cases)")}
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'conflict_%'")
    }
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_conflict_%'"
        )
    }

    assert {"namespace_key", "group_key", "generation", "revision", "overflow"} <= case_columns
    assert {"conflict_case_candidates", "conflict_candidate_members", "conflict_review_state"} <= tables
    assert {
        "idx_conflict_open_group_unique",
        "idx_conflict_review_dirty",
        "idx_conflict_review_left_tip",
        "idx_conflict_review_right_tip",
    } <= indexes
    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='045_conflict_review_queue'").fetchone()


def test_migration_045_seeds_only_unsettled_review_work(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    legacy = _pre_045_database(path)
    for prefix, slot, key in (
        ("manual", "config.path", "path-group"),
        ("pending", "config.network", "network-group"),
        ("terminal", "config.path", "terminal-group"),
    ):
        _insert_claim(legacy, f"{prefix}-left", value="left", slot=slot, conflict_key=key)
        _insert_claim(legacy, f"{prefix}-right", value="right", slot=slot, conflict_key=key)
    _insert_case(legacy, "manual", "manual-left", "manual-right")
    _insert_case(legacy, "pending", "pending-left", "pending-right", status="pending")
    _insert_case(legacy, "terminal", "terminal-left", "terminal-right", status="resolved")
    legacy.commit()
    legacy.close()

    database = Database(path)
    try:
        upgraded = database.open()
        rows = {
            row["case_id"]: dict(row)
            for row in upgraded.execute(
                "SELECT case_id,dirty_at,dirty_reason FROM conflict_review_state ORDER BY case_id"
            )
        }

        assert set(rows) == {"manual", "pending"}
        assert rows["manual"]["dirty_at"] is None
        assert rows["manual"]["dirty_reason"] == "migration_manual_clean"
        assert rows["pending"]["dirty_at"] is not None
        assert rows["pending"]["dirty_reason"] == "migration_open_case"
    finally:
        database.close()


def test_migration_045_backfills_exclusive_group_candidates(tmp_path: Path) -> None:
    path = tmp_path / "exclusive-upgrade.db"
    legacy = _pre_045_database(path)
    _insert_claim(legacy, "left", value="8080")
    _insert_claim(legacy, "right", value="8081")
    _insert_case(legacy, "case", "left", "right")
    legacy.commit()
    legacy.close()

    database = Database(path)
    try:
        upgraded = database.open()
        case = upgraded.execute(
            "SELECT namespace_key,group_key,generation,revision FROM conflict_cases WHERE id='case'"
        ).fetchone()
        candidates = upgraded.execute(
            "SELECT candidate_key,canonical_value_json,representative_claim_id,support_count "
            "FROM conflict_case_candidates WHERE case_id='case' ORDER BY candidate_key"
        ).fetchall()
        members = upgraded.execute(
            "SELECT claim_id FROM conflict_candidate_members WHERE case_id='case' ORDER BY claim_id"
        ).fetchall()

        assert tuple(case) == ("default", "service-port", 1, 0)
        assert [tuple(row) for row in candidates] == [
            ('"8080"', '"8080"', "left", 1),
            ('"8081"', '"8081"', "right", 1),
        ]
        assert [row[0] for row in members] == ["left", "right"]
    finally:
        database.close()


def test_migration_045_candidate_set_changes_bump_revision_and_dirty(tmp_path: Path) -> None:
    connection = Database(tmp_path / "candidate-change.db").open()
    for claim_id, value in (("left", "8080"), ("right", "8081"), ("third", "8082")):
        _insert_claim(connection, claim_id, value=value)
    _insert_case(
        connection,
        "case",
        "left",
        "right",
        namespace="default",
        group_key="service-port",
    )
    connection.execute("UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='test_clean' WHERE case_id='case'")

    connection.execute(
        "INSERT INTO conflict_case_candidates("
        "case_id,candidate_key,canonical_value_json,representative_claim_id,first_seen_at,last_seen_at"
        ") VALUES ('case','\"8082\"','\"8082\"','third',?,?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO conflict_candidate_members(case_id,candidate_key,claim_id,attached_at) "
        "VALUES ('case','\"8082\"','third',?)",
        (NOW,),
    )

    case = connection.execute("SELECT revision FROM conflict_cases WHERE id='case'").fetchone()
    review = connection.execute(
        "SELECT dirty_at,dirty_reason FROM conflict_review_state WHERE case_id='case'"
    ).fetchone()
    assert case[0] == 1
    assert review[0] is not None
    assert review[1] == "candidate_set_changed"
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO conflict_case_candidates("
            "case_id,candidate_key,canonical_value_json,representative_claim_id,first_seen_at,last_seen_at"
            ") VALUES ('case','\"8082\"','\"8082\"','third',?,?)",
            (NOW, NOW),
        )


def test_migration_045_claim_real_change_requeues_but_same_value_update_does_not(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-change.db").open()
    _insert_claim(connection, "left", value="8080")
    _insert_claim(connection, "right", value="8081")
    _insert_case(
        connection,
        "case",
        "left",
        "right",
        namespace="default",
        group_key="service-port",
    )
    connection.execute("UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='test_clean' WHERE case_id='case'")

    connection.execute("UPDATE claims SET source_authority=source_authority WHERE id='left'")
    assert connection.execute("SELECT dirty_at FROM conflict_review_state WHERE case_id='case'").fetchone()[0] is None

    connection.execute("UPDATE claims SET source_authority='high' WHERE id='left'")
    review = connection.execute(
        "SELECT dirty_at,dirty_reason FROM conflict_review_state WHERE case_id='case'"
    ).fetchone()
    assert review[0] is not None
    assert review[1] == "claim_input_changed"


def test_migration_045_allows_only_one_open_case_per_namespace_group(tmp_path: Path) -> None:
    connection = Database(tmp_path / "unique-open-group.db").open()
    for claim_id, value in (("left", "8080"), ("right", "8081"), ("third", "8082")):
        _insert_claim(connection, claim_id, value=value)
    _insert_case(
        connection,
        "first",
        "left",
        "right",
        namespace="default",
        group_key="service-port",
    )

    with pytest.raises(sqlite3.IntegrityError):
        _insert_case(
            connection,
            "second",
            "left",
            "third",
            namespace="default",
            group_key="service-port",
        )

    connection.execute(
        "UPDATE conflict_cases SET status='resolved',resolved_at=? WHERE id='first'",
        (NOW,),
    )
    _insert_case(
        connection,
        "second",
        "left",
        "third",
        namespace="default",
        group_key="service-port",
    )
    assert connection.execute("SELECT count(*) FROM conflict_cases WHERE group_key='service-port'").fetchone()[0] == 2
