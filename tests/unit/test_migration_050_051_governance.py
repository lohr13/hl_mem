from __future__ import annotations

import sqlite3
from pathlib import Path

from hl_mem.application.conflicts import CONFLICT_AUTO_POLICY_VERSION, upgrade_conflict_auto_policy
from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"
NOW = "2026-08-25T08:00:00+00:00"


def _schema_before_050(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for migration in sorted(MIGRATION_DIR.glob("*.sql")):
        if migration.name >= "050_":
            break
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    connection.commit()
    return connection


def _insert_open_manual_case(connection: sqlite3.Connection, case_id: str) -> None:
    claim = {
        "namespace_key": "default",
        "subject_entity_id": "gateway",
        "predicate": "配置",
        "value_json": '"8080"',
        "canonical_attribute": "config.port",
        "canonical_slot": "config.port",
        "recorded_from": NOW,
        "status": "disputed",
        "source_authority": "medium",
        "scope": "permanent",
        "volatility": "stable",
    }
    for side in ("left", "right"):
        connection.execute(
            "INSERT INTO claims(id,fact_hash,conflict_key,conflict_key_version,qualifiers_json,"
            "namespace_key,subject_entity_id,predicate,value_json,canonical_attribute,canonical_slot,"
            "recorded_from,status,source_authority,scope,volatility) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{case_id}-{side}",
                f"hash-{case_id}-{side}",
                f"key-{case_id}",
                3,
                '{"service":"gateway"}',
                *claim.values(),
            ),
        )
    connection.execute(
        "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,"
        "created_at,namespace_key,group_key) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            f"pair-{case_id}",
            f"{case_id}-left",
            f"{case_id}-right",
            "manual_required",
            "uncertain",
            NOW,
            "default",
            f"key-{case_id}",
        ),
    )


def test_fresh_schema_contains_governance_and_conflict_policy_columns(tmp_path: Path) -> None:
    connection = Database(tmp_path / "fresh.db").open()

    action_columns = {row[1] for row in connection.execute("PRAGMA table_info(governance_actions)")}
    case_columns = {row[1] for row in connection.execute("PRAGMA table_info(conflict_cases)")}
    review_columns = {row[1] for row in connection.execute("PRAGMA table_info(conflict_review_state)")}

    assert {
        "input_fingerprint",
        "policy_version",
        "before_json",
        "after_json",
        "status",
    } <= action_columns
    assert {"policy_version", "last_tier", "last_decision_hash", "resolution_rule", "resolver_model"} <= case_columns
    assert "policy_version" in review_columns
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_from_049_dirties_stable_manual_once(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    legacy = _schema_before_050(path)
    _insert_open_manual_case(legacy, "case-stable")
    legacy.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='migration_manual_clean' "
        "WHERE case_id='case-stable'"
    )
    legacy.commit()
    legacy.close()

    database = Database(path)
    upgraded = database.open()
    state = upgraded.execute(
        "SELECT dirty_at,dirty_reason,policy_version FROM conflict_review_state " "WHERE case_id='case-stable'"
    ).fetchone()
    first_dirty_at = state["dirty_at"]

    database.close()
    reopened = Database(path).open()
    state_again = reopened.execute(
        "SELECT dirty_at,dirty_reason,policy_version FROM conflict_review_state " "WHERE case_id='case-stable'"
    ).fetchone()
    assert first_dirty_at is not None
    assert tuple(state) == (first_dirty_at, "v030_policy_upgrade", CONFLICT_AUTO_POLICY_VERSION)
    assert tuple(state_again) == tuple(state)
    assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []


def test_runtime_policy_upgrade_is_idempotent_for_clean_stable_manual(tmp_path: Path) -> None:
    connection = Database(tmp_path / "policy.db").open()
    _insert_open_manual_case(connection, "case-runtime")
    connection.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='reviewed_clean',policy_version=NULL "
        "WHERE case_id='case-runtime'"
    )
    connection.commit()

    first = upgrade_conflict_auto_policy(connection, NOW)
    second = upgrade_conflict_auto_policy(connection, "2026-08-25T09:00:00+00:00")

    state = connection.execute(
        "SELECT dirty_at,dirty_reason,policy_version FROM conflict_review_state " "WHERE case_id='case-runtime'"
    ).fetchone()
    assert first == 1
    assert second == 0
    assert tuple(state) == (NOW, "v030_policy_upgrade", CONFLICT_AUTO_POLICY_VERSION)
