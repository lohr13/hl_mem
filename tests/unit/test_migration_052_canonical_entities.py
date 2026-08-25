from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.database import Database

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"
NOW = "2026-08-25T10:00:00+00:00"


def _schema_before_052(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "052_"]
    assert migrations[-1].name == "051_conflict_auto_policy.sql"
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


def _insert_entity(
    connection: sqlite3.Connection,
    entity_id: str,
    entity_type: str,
    canonical_key: str,
    *,
    namespace_key: str = "default",
) -> None:
    connection.execute(
        "INSERT INTO canonical_entities("
        "id,namespace_key,entity_type,canonical_key,display_name,status,created_at,updated_at"
        ") VALUES (?,?,?,?,?,'active',?,?)",
        (entity_id, namespace_key, entity_type, canonical_key, entity_id, NOW, NOW),
    )


def _insert_alias(
    connection: sqlite3.Connection,
    alias_id: str,
    alias: str,
    entity_type: str,
    canonical_entity_id: str,
    version: int,
    *,
    valid_to: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO entity_aliases("
        "id,namespace_key,alias_normalized,entity_type,canonical_entity_id,version,source_kind,"
        "source_event_id,valid_from,valid_to,created_at"
        ") VALUES (?, 'default', ?, ?, ?, ?, 'builtin', NULL, ?, ?, ?)",
        (alias_id, alias, entity_type, canonical_entity_id, version, NOW, valid_to, NOW),
    )


def test_fresh_schema_has_exact_entity_tables_hot_columns_and_foreign_keys(tmp_path: Path) -> None:
    connection = Database(tmp_path / "fresh.db").open()

    expected_columns = {
        "canonical_entities": {
            "id",
            "namespace_key",
            "entity_type",
            "canonical_key",
            "display_name",
            "status",
            "created_at",
            "updated_at",
        },
        "entity_aliases": {
            "id",
            "namespace_key",
            "alias_normalized",
            "entity_type",
            "canonical_entity_id",
            "version",
            "source_kind",
            "source_event_id",
            "valid_from",
            "valid_to",
            "created_at",
        },
        "entity_relations": {
            "id",
            "namespace_key",
            "from_entity_id",
            "to_entity_id",
            "relation",
            "source_event_id",
            "confidence",
            "valid_from",
            "valid_to",
        },
        "claim_entity_links": {
            "claim_id",
            "canonical_entity_id",
            "role",
            "mention_text",
            "resolution_confidence",
            "alias_version",
            "proof_id",
        },
    }
    for table, columns in expected_columns.items():
        assert {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} == columns
    claim_columns = {row[1] for row in connection.execute("PRAGMA table_info(claims)")}
    assert {"subject_canonical_entity_id", "canonical_target_entity_id"} <= claim_columns
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='052_canonical_entities'"
    ).fetchone()


def test_upgrade_from_051_preserves_claim_and_adds_nullable_hot_columns(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    legacy = _schema_before_052(path)
    legacy.execute(
        "INSERT INTO claims(id,subject_entity_id,value_json,recorded_from,status) "
        "VALUES ('legacy','legacy subject','\"value\"',?,'active')",
        (NOW,),
    )
    legacy.commit()
    legacy.close()

    upgraded = Database(path).open()

    row = upgraded.execute(
        "SELECT subject_entity_id,subject_canonical_entity_id,canonical_target_entity_id "
        "FROM claims WHERE id='legacy'"
    ).fetchone()
    assert tuple(row) == ("legacy subject", None, None)
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []


def test_only_one_active_alias_is_allowed_per_namespace_type_and_normalized_alias(tmp_path: Path) -> None:
    connection = Database(tmp_path / "active-alias.db").open()
    _insert_entity(connection, "agent:first", "agent", "first")
    _insert_entity(connection, "agent:second", "agent", "second")
    _insert_alias(connection, "alias-1", "shared", "agent", "agent:first", 1)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_alias(connection, "alias-2", "shared", "agent", "agent:second", 2)

    connection.execute(
        "UPDATE entity_aliases SET valid_to='2026-08-25T11:00:00+00:00' WHERE id='alias-1'"
    )
    _insert_alias(connection, "alias-2", "shared", "agent", "agent:second", 2)


def test_alias_target_must_exist_and_match_namespace_and_type(tmp_path: Path) -> None:
    connection = Database(tmp_path / "alias-target.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")

    with pytest.raises(sqlite3.IntegrityError, match="entity alias target type or namespace mismatch"):
        _insert_alias(connection, "wrong-type", "本地小马", "environment", "agent:local_pony", 1)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_alias(connection, "alias-chain", "小马", "agent", "alias:other", 1)

    _insert_alias(connection, "valid", "本地小马", "agent", "agent:local_pony", 1)
    with pytest.raises(sqlite3.IntegrityError, match="canonical entity coordinates are immutable"):
        connection.execute(
            "UPDATE canonical_entities SET namespace_key='other' WHERE id='agent:local_pony'"
        )


@pytest.mark.parametrize("relation", ["same_as", "located_at", "supports"])
def test_entity_relation_vocabulary_is_restrictive(tmp_path: Path, relation: str) -> None:
    connection = Database(tmp_path / f"relation-{relation}.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    _insert_entity(connection, "device:user_local_pc", "device", "user_local_pc")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "INSERT INTO entity_relations("
            "id,namespace_key,from_entity_id,to_entity_id,relation,confidence,valid_from"
            ") VALUES ('relation','default','agent:local_pony','device:user_local_pc',?,?,?)",
            (relation, 1.0, NOW),
        )


def test_claim_link_rejects_topic_subject_and_cross_type_roles(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-link-types.db").open()
    connection.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES ('claim','\"value\"',?,'active')",
        (NOW,),
    )
    _insert_entity(connection, "topic:memory", "topic", "memory")
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")

    for entity_id, role in (("topic:memory", "subject"), ("agent:local_pony", "environment")):
        with pytest.raises(sqlite3.IntegrityError, match="claim entity role/type mismatch"):
            connection.execute(
                "INSERT INTO claim_entity_links("
                "claim_id,canonical_entity_id,role,mention_text,resolution_confidence"
                ") VALUES ('claim',?,?,?,1.0)",
                (entity_id, role, entity_id),
            )


def test_claim_link_rejects_cross_namespace_binding(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-link-namespace.db").open()
    connection.execute(
        "INSERT INTO claims(id,namespace_key,value_json,recorded_from,status) "
        "VALUES ('claim','default','\"value\"',?,'active')",
        (NOW,),
    )
    _insert_entity(
        connection,
        "environment:other_runtime",
        "environment",
        "other_runtime",
        namespace_key="other",
    )

    with pytest.raises(sqlite3.IntegrityError, match="claim entity namespace mismatch"):
        connection.execute(
            "INSERT INTO claim_entity_links("
            "claim_id,canonical_entity_id,role,mention_text,resolution_confidence"
            ") VALUES ('claim','environment:other_runtime','environment','runtime',1.0)"
        )


def test_claim_hot_columns_reject_topic_subject_and_cross_namespace_target(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-hot-columns.db").open()
    _insert_entity(connection, "topic:memory", "topic", "memory")
    _insert_entity(
        connection,
        "environment:other_runtime",
        "environment",
        "other_runtime",
        namespace_key="other",
    )

    with pytest.raises(sqlite3.IntegrityError, match="claim canonical subject type or namespace mismatch"):
        connection.execute(
            "INSERT INTO claims("
            "id,namespace_key,value_json,recorded_from,status,subject_canonical_entity_id"
            ") VALUES ('topic-subject','default','\"value\"',?,'active','topic:memory')",
            (NOW,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="claim canonical target namespace mismatch"):
        connection.execute(
            "INSERT INTO claims("
            "id,namespace_key,value_json,recorded_from,status,canonical_target_entity_id"
            ") VALUES ('cross-target','default','\"value\"',?,'active','environment:other_runtime')",
            (NOW,),
        )
