from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository

NOW = "2026-08-25T10:00:00+00:00"


def _connection(tmp_path: Path):
    return Database(tmp_path / "entity-resolution.db").open()


def _claim(subject: str, value: str = "8080") -> ExtractedClaim:
    return ExtractedClaim(
        subject=subject,
        predicate="configures",
        value=value,
        canonical_attribute="config.port",
        canonical_slot="config.port",
        qualifiers={"service": "api"},
    )


def _store(connection, subject: str, value: str = "8080"):
    return IngestService.store_extracted(
        connection,
        _claim(subject, value),
        {"id": f"event-{subject}-{value}", "tenant_id": "default", "actor_type": "user"},
        NOW,
        FakeEmbedder(8),
    )


def test_ingest_dual_writes_typed_subject_and_claim_link_in_one_transaction(tmp_path: Path) -> None:
    connection = _connection(tmp_path)

    result = _store(connection, "user")

    claim = ClaimRepository(connection).get_claim(str(result.claim_id))
    assert claim["subject_entity_id"] == "user"
    assert claim["subject_canonical_entity_id"] == "person:user"
    assert claim["conflict_key_version"] == 4
    link = connection.execute(
        "SELECT canonical_entity_id,role,mention_text,alias_version,proof_id "
        "FROM claim_entity_links WHERE claim_id=?",
        (result.claim_id,),
    ).fetchone()
    assert tuple(link)[:4] == ("person:user", "subject", "user", 1)
    assert connection.execute(
        "SELECT 1 FROM evidence_links WHERE id=? AND derived_id=?",
        (link["proof_id"], result.claim_id),
    ).fetchone()
    assert connection.execute(
        "SELECT 1 FROM entity_aliases WHERE namespace_key='default' "
        "AND canonical_entity_id='person:user' AND source_kind='builtin'"
    ).fetchone()


def test_ingest_without_active_proof_keeps_nullable_projection(tmp_path: Path) -> None:
    connection = _connection(tmp_path)

    result = _store(connection, "unregistered subject")

    claim = ClaimRepository(connection).get_claim(str(result.claim_id))
    assert claim["subject_entity_id"] == "unregistered subject"
    assert claim["subject_canonical_entity_id"] is None
    assert claim["canonical_target_entity_id"] is None
    assert claim["conflict_key_version"] == 4
    assert (
        connection.execute("SELECT count(*) FROM claim_entity_links WHERE claim_id=?", (result.claim_id,)).fetchone()[0]
        == 0
    )


def test_ingest_builtin_bootstrap_does_not_override_explicit_alias_correction(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    entities = EntityRepository(connection)
    entities.seed_builtins(now=NOW)
    entities.create_entity("person:operator", "person", "operator", "Operator", now=NOW)
    entities.close_alias("user", "person", valid_to="2026-08-25T10:01:00+00:00")
    entities.create_alias(
        "user",
        "person",
        "person:operator",
        "user_explicit",
        valid_from="2026-08-25T10:01:00+00:00",
    )
    connection.commit()

    result = _store(connection, "user")

    claim = ClaimRepository(connection).get_claim(str(result.claim_id))
    assert claim["subject_canonical_entity_id"] == "person:operator"
    assert (
        connection.execute(
            "SELECT count(*) FROM entity_aliases WHERE alias_normalized='user' AND valid_to IS NULL"
        ).fetchone()[0]
        == 1
    )


def test_conflict_key_v4_separates_typed_and_tagged_legacy_coordinates() -> None:
    common = ("default", "person:user", "uses", "config.port", {"service": "api"})
    typed = compute_conflict_key(*common, version=4, subject_canonical_entity_id="person:user")
    typed_alias = compute_conflict_key(
        "default",
        "the current user",
        "uses",
        "config.port",
        {"service": "api"},
        version=4,
        subject_canonical_entity_id="person:user",
    )
    legacy_same_text = compute_conflict_key(*common, version=4)
    legacy_other = compute_conflict_key(
        "default", "agent:person:user", "uses", "config.port", {"service": "api"}, version=4
    )

    assert typed == typed_alias
    assert typed != legacy_same_text != legacy_other
    assert compute_conflict_key(*common, version=3) == compute_conflict_key(*common)


def test_rekey_collision_uses_existing_group_conflict_pipeline(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    entities = EntityRepository(connection)
    entities.seed_builtins(now=NOW)
    connection.commit()
    left = _store(connection, "user", "8080")
    right = _store(connection, "operator", "9090")
    repository = ClaimRepository(connection)
    left_claim = repository.get_claim(str(left.claim_id))
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    entities.create_alias("operator", "person", "person:user", "user_explicit", valid_from=NOW)
    connection.execute(
        "UPDATE claims SET subject_canonical_entity_id=NULL,conflict_key=?,conflict_key_version=3 WHERE id=?",
        ("legacy-right", right.claim_id),
    )
    connection.commit()

    outcome = repository.rekey_canonical_subject(
        str(right.claim_id),
        "person:user",
        str(left_claim["conflict_key"]),
        expected_conflict_key="legacy-right",
        expected_version=3,
        changed_at=NOW,
    )

    assert outcome == "quarantined"
    assert {row["status"] for row in repository.find_by_conflict_key(left_claim["conflict_key"])} == {"disputed"}
    case = connection.execute("SELECT group_key,rationale FROM conflict_cases").fetchone()
    assert tuple(case) == (left_claim["conflict_key"], "entity_rekey_collision")
    assert (
        repository.rekey_canonical_subject(
            str(right.claim_id),
            "person:user",
            str(left_claim["conflict_key"]),
            expected_conflict_key="legacy-right",
            expected_version=3,
            changed_at=NOW,
        )
        == "stale"
    )


def test_rekey_can_join_a_caller_transaction_and_roll_back(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    EntityRepository(connection).seed_builtins(now=NOW)
    connection.commit()
    stored = _store(connection, "legacy operator")
    repository = ClaimRepository(connection)
    before = repository.get_claim(str(stored.claim_id))
    connection.execute("BEGIN IMMEDIATE")

    outcome = repository.rekey_canonical_subject(
        str(stored.claim_id),
        "person:user",
        "replacement-v4-key",
        expected_conflict_key=before["conflict_key"],
        expected_version=4,
        changed_at=NOW,
        commit=False,
    )
    connection.rollback()

    assert outcome == "updated"
    after = repository.get_claim(str(stored.claim_id))
    assert after["subject_canonical_entity_id"] is None
    assert after["conflict_key"] == before["conflict_key"]


def test_collision_rekey_rollback_restores_claims_and_removes_case(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    entities = EntityRepository(connection)
    entities.seed_builtins(now=NOW)
    connection.commit()
    left = _store(connection, "user", "8080")
    right = _store(connection, "operator", "9090")
    repository = ClaimRepository(connection)
    left_before = repository.get_claim(str(left.claim_id))
    right_before = repository.get_claim(str(right.claim_id))
    entities.create_alias("operator", "person", "person:user", "user_explicit", valid_from=NOW)
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")

    outcome = repository.rekey_canonical_subject(
        str(right.claim_id),
        "person:user",
        left_before["conflict_key"],
        expected_conflict_key=right_before["conflict_key"],
        expected_version=4,
        changed_at=NOW,
        commit=False,
    )
    assert outcome == "quarantined"
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1
    connection.rollback()

    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    assert repository.get_claim(str(left.claim_id))["status"] == "active"
    right_after = repository.get_claim(str(right.claim_id))
    assert right_after["status"] == "active"
    assert right_after["conflict_key"] == right_before["conflict_key"]
    assert right_after["subject_canonical_entity_id"] is None


def test_collision_rekey_failure_rolls_back_partial_mutations(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    entities = EntityRepository(connection)
    entities.seed_builtins(now=NOW)
    connection.commit()
    left = _store(connection, "user", "8080")
    right = _store(connection, "operator", "9090")
    repository = ClaimRepository(connection)
    left_before = repository.get_claim(str(left.claim_id))
    right_before = repository.get_claim(str(right.claim_id))
    entities.create_alias("operator", "person", "person:user", "user_explicit", valid_from=NOW)
    connection.execute(
        "CREATE TRIGGER fail_entity_rekey_case BEFORE INSERT ON conflict_cases "
        "BEGIN SELECT RAISE(ABORT, 'forced entity rekey failure'); END"
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced entity rekey failure"):
        repository.rekey_canonical_subject(
            str(right.claim_id),
            "person:user",
            left_before["conflict_key"],
            expected_conflict_key=right_before["conflict_key"],
            expected_version=4,
            changed_at=NOW,
        )

    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    assert repository.get_claim(str(left.claim_id))["status"] == "active"
    right_after = repository.get_claim(str(right.claim_id))
    assert right_after["status"] == "active"
    assert right_after["conflict_key"] == right_before["conflict_key"]
    assert right_after["subject_canonical_entity_id"] is None
