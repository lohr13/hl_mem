from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository
from hl_mem.storage.migrations.backfill_canonical_entities_v1 import (
    audit_canonical_entity_backfill,
    prepare_canonical_entity_audit_clone,
)

NOW = "2026-08-25T10:00:00+00:00"


def _insert_claim(connection, claim_id: str, subject: str) -> None:
    connection.execute(
        "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
        "canonical_slot,conflict_key,conflict_key_version,recorded_from,status) "
        "VALUES (?,'default',?,'configures','\"8080\"','{\"service\":\"api\"}',"
        "'config.port',?,3,?,'active')",
        (claim_id, subject, f"legacy-{claim_id}", NOW),
    )


def test_backfill_audit_is_bounded_read_only_and_cursor_stable(tmp_path: Path) -> None:
    connection = Database(tmp_path / "entity-backfill.db").open()
    assert connection.execute("SELECT count(*) FROM canonical_entities").fetchone()[0] == 0
    prepare_canonical_entity_audit_clone(connection, now=NOW)
    _insert_claim(connection, "a", "user")
    _insert_claim(connection, "b", "unknown")
    _insert_claim(connection, "c", "user")
    connection.commit()
    before_changes = connection.total_changes

    first = audit_canonical_entity_backfill(connection, limit=2)
    replay = audit_canonical_entity_backfill(connection, limit=2)
    second = audit_canonical_entity_backfill(connection, cursor=first.next_cursor, limit=2)

    assert first == replay
    assert [record.claim_id for record in first.records] == ["a", "b"]
    assert [record.outcome for record in first.records] == ["collision", "no_proof"]
    assert [record.claim_id for record in second.records] == ["c"]
    assert [record.outcome for record in second.records] == ["collision"]
    assert first.next_cursor == "b" and second.done is True
    assert connection.total_changes == before_changes
    assert (
        connection.execute("SELECT count(*) FROM claims WHERE subject_canonical_entity_id IS NOT NULL").fetchone()[0]
        == 0
    )


def test_backfill_reports_type_mismatch_without_guessing(tmp_path: Path) -> None:
    connection = Database(tmp_path / "entity-backfill-types.db").open()
    entities = EntityRepository(connection)
    entities.create_entity("agent:shared", "agent", "shared", "Agent", now=NOW)
    entities.create_entity("environment:shared", "environment", "shared", "Environment", now=NOW)
    entities.create_alias("shared", "agent", "agent:shared", "user_explicit", valid_from=NOW)
    entities.create_alias("shared", "environment", "environment:shared", "user_explicit", valid_from=NOW)
    _insert_claim(connection, "ambiguous", "shared")
    connection.commit()

    result = audit_canonical_entity_backfill(connection, limit=10)

    assert [(record.claim_id, record.outcome) for record in result.records] == [("ambiguous", "type_mismatch")]
    assert (
        connection.execute("SELECT subject_canonical_entity_id FROM claims WHERE id='ambiguous'").fetchone()[0] is None
    )


def test_backfill_reports_colliding_proposed_v4_groups(tmp_path: Path) -> None:
    connection = Database(tmp_path / "entity-backfill-collision.db").open()
    entities = EntityRepository(connection)
    entities.seed_builtins(now=NOW)
    entities.create_alias("operator", "person", "person:user", "user_explicit", valid_from=NOW)
    _insert_claim(connection, "left", "user")
    _insert_claim(connection, "right", "operator")
    connection.commit()

    first = audit_canonical_entity_backfill(connection, limit=1)
    second = audit_canonical_entity_backfill(connection, cursor=first.next_cursor, limit=1)

    assert [record.outcome for record in first.records] == ["collision"]
    assert [record.outcome for record in second.records] == ["collision"]


def test_backfill_cursor_progresses_beyond_five_hundred_unresolved_claims(tmp_path: Path) -> None:
    connection = Database(tmp_path / "entity-backfill-large.db").open()
    for index in range(501):
        _insert_claim(connection, f"claim-{index:03d}", f"unknown-{index:03d}")
    connection.commit()

    first = audit_canonical_entity_backfill(connection, limit=500)
    second = audit_canonical_entity_backfill(connection, cursor=first.next_cursor, limit=500)

    assert len(first.records) == 500 and first.done is False
    assert [record.claim_id for record in second.records] == ["claim-500"]
    assert second.done is True and second.next_cursor == "claim-500"
