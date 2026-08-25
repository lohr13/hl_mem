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
    namespace_key: str = "default",
    source_event_id: str | None = None,
    valid_to: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO entity_aliases("
        "id,namespace_key,alias_normalized,entity_type,canonical_entity_id,version,source_kind,"
        "source_event_id,valid_from,valid_to,created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, 'builtin', ?, ?, ?, ?)",
        (
            alias_id,
            namespace_key,
            alias,
            entity_type,
            canonical_entity_id,
            version,
            source_event_id,
            NOW,
            valid_to,
            NOW,
        ),
    )


def _insert_event(connection: sqlite3.Connection, event_id: str, tenant_id: str) -> None:
    connection.execute(
        "INSERT INTO events("
        "id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at"
        ") VALUES (?,?, 'message','user','{}',?,?)",
        (event_id, tenant_id, NOW, NOW),
    )


def _insert_claim_link(
    connection: sqlite3.Connection,
    claim_id: str,
    entity_id: str,
    mention: str,
    *,
    role: str = "actor",
) -> None:
    connection.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?, '\"value\"',?,'active')",
        (claim_id, NOW),
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES (?,'claim',?,'event','event','supports')",
        (f"{claim_id}-proof", claim_id),
    )
    connection.execute(
        "INSERT INTO claim_entity_links("
        "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
        ") VALUES (?,?,?,?,1.0,1,?)",
        (claim_id, entity_id, role, mention, f"{claim_id}-proof"),
    )


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
    grouped: dict[int, tuple[str, list[tuple[str, str]]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
        target, columns = grouped.setdefault(int(row[0]), (str(row[2]), []))
        columns.append((str(row[3]), str(row[4])))
    return sorted((target, tuple(columns)) for target, columns in grouped.values())


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
    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='052_canonical_entities'").fetchone()


def test_migration_declares_composite_and_evidence_foreign_keys(tmp_path: Path) -> None:
    connection = Database(tmp_path / "foreign-keys.db").open()

    assert _foreign_keys(connection, "entity_aliases") == sorted(
        [
            (
                "canonical_entities",
                (
                    ("namespace_key", "namespace_key"),
                    ("entity_type", "entity_type"),
                    ("canonical_entity_id", "id"),
                ),
            ),
            ("events", (("namespace_key", "tenant_id"), ("source_event_id", "id"))),
        ]
    )
    assert _foreign_keys(connection, "entity_relations") == sorted(
        [
            (
                "canonical_entities",
                (("namespace_key", "namespace_key"), ("from_entity_id", "id")),
            ),
            (
                "canonical_entities",
                (("namespace_key", "namespace_key"), ("to_entity_id", "id")),
            ),
            ("events", (("namespace_key", "tenant_id"), ("source_event_id", "id"))),
        ]
    )
    assert _foreign_keys(connection, "claim_entity_links") == sorted(
        [
            ("claims", (("claim_id", "id"),)),
            ("evidence_links", (("proof_id", "id"),)),
        ]
    )

    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name IN "
            "('entity_aliases','entity_relations','claim_entity_links')"
        )
    }
    assert {
        "idx_entity_aliases_source_event",
        "idx_entity_relations_source_event",
        "idx_claim_entity_links_proof",
    } <= indexes


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

    connection.execute("UPDATE entity_aliases SET valid_to='2026-08-25T11:00:00+00:00' WHERE id='alias-1'")
    _insert_alias(connection, "alias-2", "shared", "agent", "agent:second", 2)


def test_alias_target_must_exist_and_match_namespace_and_type(tmp_path: Path) -> None:
    connection = Database(tmp_path / "alias-target.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_alias(connection, "wrong-type", "本地小马", "environment", "agent:local_pony", 1)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_alias(connection, "alias-chain", "小马", "agent", "alias:other", 1)

    _insert_alias(connection, "valid", "本地小马", "agent", "agent:local_pony", 1)
    with pytest.raises(sqlite3.IntegrityError, match="canonical entity coordinates are immutable"):
        connection.execute("UPDATE canonical_entities SET namespace_key='other' WHERE id='agent:local_pony'")


@pytest.mark.parametrize(
    ("entity_id", "canonical_key"),
    [
        ("agent:e_short", "e_short"),
        ("agent:bad\tkey", "bad\tkey"),
        ("agent:naïve", "naïve"),
    ],
)
def test_raw_sql_rejects_ids_outside_canonical_grammar(tmp_path: Path, entity_id: str, canonical_key: str) -> None:
    connection = Database(tmp_path / "raw-id.db").open()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_entity(connection, entity_id, "agent", canonical_key)


@pytest.mark.parametrize("alias", ["Foo", "foo\tbar", "foo  bar"])
def test_raw_sql_rejects_non_normalized_ascii_aliases(tmp_path: Path, alias: str) -> None:
    connection = Database(tmp_path / "raw-alias.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_alias(connection, "raw", alias, "agent", "agent:local_pony", 1)


@pytest.mark.parametrize("alias", ["\uff26\uff4f\uff4f", "\u24bb\u24de\u24de", "\ufb00oo"])
def test_database_alias_check_uses_resolver_equivalent_nfkc(tmp_path: Path, alias: str) -> None:
    connection = Database(tmp_path / "raw-unicode-alias.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_alias(connection, "raw", alias, "agent", "agent:local_pony", 1)
    _insert_alias(connection, "chinese", "本地小马", "agent", "agent:local_pony", 1)


def test_direct_sqlite_connection_without_nfkc_function_fails_writes_closed(tmp_path: Path) -> None:
    path = tmp_path / "direct-connection.db"
    database = Database(path)
    managed = database.open()
    assert managed.execute("SELECT hl_mem_normalize_alias(' ＦＯＯ ')").fetchone()[0] == "foo"
    _insert_entity(managed, "agent:local_pony", "agent", "local_pony")
    managed.commit()
    assert database.open_readonly().execute("SELECT hl_mem_normalize_alias('本地小马')").fetchone()[0] == "本地小马"

    direct = sqlite3.connect(path)
    direct.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.OperationalError, match="unknown function: hl_mem_normalize_alias"):
        _insert_alias(direct, "raw", "pony", "agent", "agent:local_pony", 1)


def test_alias_history_is_immutable_except_one_way_close(tmp_path: Path) -> None:
    connection = Database(tmp_path / "alias-history.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    _insert_alias(connection, "alias", "pony", "agent", "agent:local_pony", 1)

    for assignment in (
        "id='other'",
        "namespace_key='other'",
        "alias_normalized='horse'",
        "entity_type='environment'",
        "version=2",
        "source_kind='migration_exact'",
        "source_event_id='event-x'",
        "valid_from='2026-08-25T09:00:00+00:00'",
        "created_at='2026-08-25T09:00:00+00:00'",
        "canonical_entity_id='agent:other'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="entity alias history is immutable"):
            connection.execute(f"UPDATE entity_aliases SET {assignment} WHERE id='alias'")

    connection.execute("UPDATE entity_aliases SET valid_to=? WHERE id='alias'", ("2026-08-25T11:00:00+00:00",))
    for assignment in ("valid_to=NULL", "valid_to='2026-08-25T12:00:00+00:00'"):
        with pytest.raises(sqlite3.IntegrityError, match="entity alias history is immutable"):
            connection.execute(f"UPDATE entity_aliases SET {assignment} WHERE id='alias'")


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


def test_relation_endpoints_and_source_event_are_namespace_scoped_and_history_is_immutable(
    tmp_path: Path,
) -> None:
    connection = Database(tmp_path / "relation-namespace.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony", namespace_key="tenant-a")
    _insert_entity(
        connection,
        "device:user_local_pc",
        "device",
        "user_local_pc",
        namespace_key="tenant-a",
    )
    _insert_event(connection, "event-b", "tenant-b")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_alias(
            connection,
            "alias",
            "pony",
            "agent",
            "agent:local_pony",
            1,
            namespace_key="tenant-a",
            source_event_id="event-b",
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        connection.execute(
            "INSERT INTO entity_relations("
            "id,namespace_key,from_entity_id,to_entity_id,relation,source_event_id,confidence,valid_from"
            ") VALUES ('relation','tenant-a','agent:local_pony','device:user_local_pc',"
            "'runs_on','event-b',1.0,?)",
            (NOW,),
        )

    _insert_event(connection, "event-a", "tenant-a")
    connection.execute(
        "INSERT INTO entity_relations("
        "id,namespace_key,from_entity_id,to_entity_id,relation,source_event_id,confidence,valid_from"
        ") VALUES ('relation','tenant-a','agent:local_pony','device:user_local_pc',"
        "'runs_on','event-a',1.0,?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="entity relation history is immutable"):
        connection.execute("UPDATE entity_relations SET confidence=0.5 WHERE id='relation'")


def test_claim_link_rejects_topic_subject_and_cross_type_roles(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-link-types.db").open()
    connection.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES ('claim','\"value\"',?,'active')",
        (NOW,),
    )
    _insert_entity(connection, "topic:memory", "topic", "memory")
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    _insert_alias(connection, "topic-alias", "memory", "topic", "topic:memory", 1)
    _insert_alias(connection, "agent-alias", "pony", "agent", "agent:local_pony", 1)
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES ('proof','claim','claim','event','event','supports')"
    )

    for entity_id, role, mention in (
        ("topic:memory", "subject", "memory"),
        ("agent:local_pony", "environment", "pony"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="claim entity alias or evidence proof mismatch"):
            connection.execute(
                "INSERT INTO claim_entity_links("
                "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
                ") VALUES ('claim',?,?,?,1.0,1,'proof')",
                (entity_id, role, mention),
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
    _insert_alias(
        connection,
        "other-alias",
        "runtime",
        "environment",
        "environment:other_runtime",
        1,
        namespace_key="other",
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES ('proof','claim','claim','event','event','supports')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="claim entity alias or evidence proof mismatch"):
        connection.execute(
            "INSERT INTO claim_entity_links("
            "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
            ") VALUES ('claim','environment:other_runtime','environment','runtime',1.0,1,'proof')"
        )


def test_raw_claim_link_requires_matching_alias_version_mention_and_claim_proof(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-link-proof.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    _insert_alias(connection, "pony-alias", "pony", "agent", "agent:local_pony", 1)
    for claim_id in ("no-version", "no-proof", "bad-mention", "bad-version", "bad-proof", "other"):
        connection.execute(
            "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?, '\"value\"',?,'active')",
            (claim_id, NOW),
        )
    for proof_id, claim_id in (
        ("proof-no-version", "no-version"),
        ("proof-bad-mention", "bad-mention"),
        ("proof-bad-version", "bad-version"),
        ("proof-other", "other"),
    ):
        connection.execute(
            "INSERT INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation"
            ") VALUES (?,'claim',?,'event','event','supports')",
            (proof_id, claim_id),
        )

    invalid = (
        ("no-version", "pony", None, "proof-no-version"),
        ("no-proof", "pony", 1, None),
        ("bad-mention", "horse", 1, "proof-bad-mention"),
        ("bad-version", "pony", 2, "proof-bad-version"),
        ("bad-proof", "pony", 1, "proof-other"),
    )
    for claim_id, mention, version, proof_id in invalid:
        with pytest.raises(sqlite3.IntegrityError, match="claim entity alias or evidence proof mismatch"):
            connection.execute(
                "INSERT INTO claim_entity_links("
                "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
                ") VALUES (?,'agent:local_pony','actor',?,1.0,?,?)",
                (claim_id, mention, version, proof_id),
            )

    connection.execute(
        "INSERT INTO claim_entity_links("
        "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
        ") VALUES ('other','agent:local_pony','actor','pony',1.0,1,'proof-other')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="claim entity link history is immutable"):
        connection.execute("UPDATE claim_entity_links SET resolution_confidence=0.5 WHERE claim_id='other'")
    with pytest.raises(sqlite3.IntegrityError, match="linked claim namespace is immutable"):
        connection.execute("UPDATE claims SET namespace_key='other' WHERE id='other'")


def test_non_nfkc_raw_alias_cannot_prove_claim_link_even_when_mention_matches(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-link-nfkc.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    _insert_alias(connection, "raw", "\uff26\uff4f\uff4f", "agent", "agent:local_pony", 1)
    connection.execute("PRAGMA ignore_check_constraints=OFF")
    connection.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES ('claim','\"value\"',?,'active')",
        (NOW,),
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES ('proof','claim','claim','event','event','supports')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            "INSERT INTO claim_entity_links("
            "claim_id,canonical_entity_id,role,mention_text,resolution_confidence,alias_version,proof_id"
            ") VALUES ('claim','agent:local_pony','actor','\uff26\uff4f\uff4f',1.0,1,'proof')"
        )


def test_alias_relation_history_is_immutable_while_claim_link_cascades_with_proof(tmp_path: Path) -> None:
    connection = Database(tmp_path / "history-delete.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    _insert_entity(connection, "device:user_local_pc", "device", "user_local_pc")
    _insert_alias(connection, "alias", "pony", "agent", "agent:local_pony", 1)
    connection.execute(
        "INSERT INTO entity_relations("
        "id,namespace_key,from_entity_id,to_entity_id,relation,confidence,valid_from"
        ") VALUES ('relation','default','agent:local_pony','device:user_local_pc','runs_on',1.0,?)",
        (NOW,),
    )
    _insert_claim_link(connection, "claim", "agent:local_pony", "pony")

    for table, predicate, message in (
        ("entity_aliases", "id='alias'", "entity alias history is immutable"),
        ("entity_relations", "id='relation'", "entity relation history is immutable"),
    ):
        with pytest.raises(sqlite3.IntegrityError, match=message):
            connection.execute(f"DELETE FROM {table} WHERE {predicate}")
    connection.execute("DELETE FROM evidence_links WHERE id='claim-proof'")
    assert connection.execute("SELECT 1 FROM claim_entity_links").fetchone() is None
    _insert_claim_link(connection, "claim-two", "agent:local_pony", "pony")
    connection.execute("DELETE FROM claims WHERE id='claim-two'")
    assert connection.execute("SELECT 1 FROM claim_entity_links").fetchone() is None
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_canonical_delete_rejects_claim_link_reverse_reference(tmp_path: Path) -> None:
    connection = Database(tmp_path / "canonical-link-delete.db").open()
    _insert_entity(connection, "agent:local_pony", "agent", "local_pony")
    _insert_alias(connection, "alias", "pony", "agent", "agent:local_pony", 1)
    _insert_claim_link(connection, "claim", "agent:local_pony", "pony")

    with pytest.raises(sqlite3.IntegrityError, match="canonical entity is referenced by a claim"):
        connection.execute("DELETE FROM canonical_entities WHERE namespace_key='default' AND id='agent:local_pony'")
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_canonical_delete_allows_ordered_cleanup_of_claim_hot_references(tmp_path: Path) -> None:
    connection = Database(tmp_path / "canonical-hot-delete.db").open()
    _insert_entity(connection, "environment:local_runtime", "environment", "local_runtime")
    connection.execute(
        "INSERT INTO claims("
        "id,value_json,recorded_from,status,subject_canonical_entity_id,canonical_target_entity_id"
        ") VALUES ('claim','\"value\"',?,'active','environment:local_runtime','environment:local_runtime')",
        (NOW,),
    )
    delete = "DELETE FROM canonical_entities " "WHERE namespace_key='default' AND id='environment:local_runtime'"

    connection.execute("UPDATE claims SET subject_canonical_entity_id=NULL WHERE id='claim'")
    with pytest.raises(sqlite3.IntegrityError, match="canonical entity is referenced by a claim"):
        connection.execute(delete)
    connection.execute(
        "UPDATE claims SET subject_canonical_entity_id='environment:local_runtime', "
        "canonical_target_entity_id=NULL WHERE id='claim'"
    )
    with pytest.raises(sqlite3.IntegrityError, match="canonical entity is referenced by a claim"):
        connection.execute(delete)
    connection.execute("UPDATE claims SET subject_canonical_entity_id=NULL WHERE id='claim'")
    connection.execute(delete)

    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_claim_hot_columns_reject_topic_subject_and_cross_namespace_target(tmp_path: Path) -> None:
    connection = Database(tmp_path / "claim-hot-columns.db").open()
    _insert_entity(connection, "topic:memory", "topic", "memory")
    _insert_entity(connection, "environment:local_runtime", "environment", "local_runtime")
    _insert_entity(
        connection,
        "environment:other_runtime",
        "environment",
        "other_runtime",
        namespace_key="other",
    )

    with pytest.raises(sqlite3.IntegrityError, match="claim canonical entity type or namespace mismatch"):
        connection.execute(
            "INSERT INTO claims("
            "id,namespace_key,value_json,recorded_from,status,subject_canonical_entity_id"
            ") VALUES ('topic-subject','default','\"value\"',?,'active','topic:memory')",
            (NOW,),
        )

    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,value_json,recorded_from,status,subject_canonical_entity_id"
        ") VALUES ('valid','default','\"value\"',?,'active','environment:local_runtime')",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="claim canonical entity type or namespace mismatch"):
        connection.execute("UPDATE claims SET subject_canonical_entity_id='topic:memory' WHERE id='valid'")
    with pytest.raises(sqlite3.IntegrityError, match="claim canonical entity type or namespace mismatch"):
        connection.execute(
            "INSERT INTO claims("
            "id,namespace_key,value_json,recorded_from,status,canonical_target_entity_id"
            ") VALUES ('cross-target','default','\"value\"',?,'active','environment:other_runtime')",
            (NOW,),
        )
