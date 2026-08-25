from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from hl_mem.application.entity_resolution import EntityResolutionService
from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.domain.entity_coordinates import EntityCoordinateError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository

NOW = "2026-08-25T10:00:00+00:00"
MIGRATION_DIR = Path(__file__).resolve().parents[2] / "src/hl_mem/storage/migrations"


def _connection(tmp_path: Path):
    return Database(tmp_path / "entity-resolution.db").open()


def _upgraded_connection(tmp_path: Path):
    path = tmp_path / "entity-resolution-upgrade.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT)")
    migrations = [migration for migration in sorted(MIGRATION_DIR.glob("*.sql")) if migration.name < "052_"]
    assert migrations[-1].name == "051_conflict_auto_policy.sql"
    for migration in migrations:
        legacy.executescript(migration.read_text(encoding="utf-8"))
        legacy.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration.stem,))
    for version in (
        "006_data_conflict_key_v2",
        "011_data_fact_hash_v2",
        "016_data_conflict_key_v3",
        "038_data_subject_canonicalization_v2",
    ):
        legacy.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    legacy.commit()
    legacy.close()
    return Database(path).open()


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


def _downgrade_to_v3(connection, claim_id: str) -> dict:
    repository = ClaimRepository(connection)
    claim = repository.get_claim(claim_id)
    key = compute_conflict_key(
        claim["namespace_key"],
        claim["subject_entity_id"],
        claim["predicate"],
        claim["canonical_slot"],
        claim["qualifiers"],
        version=3,
    )
    connection.execute("DELETE FROM claim_entity_links WHERE claim_id=?", (claim_id,))
    connection.execute(
        "UPDATE claims SET subject_canonical_entity_id=NULL,conflict_key=?,conflict_key_version=3 " "WHERE id=?",
        (key, claim_id),
    )
    connection.commit()
    return repository.get_claim(claim_id)


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


def test_ingest_rekeys_same_subject_v3_before_v4_conflict_resolution(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    existing = _store(connection, "user", "8080")
    _downgrade_to_v3(connection, str(existing.claim_id))

    incoming = _store(connection, "user", "9090")

    rows = connection.execute(
        "SELECT status,conflict_key_version,subject_canonical_entity_id FROM claims ORDER BY id"
    ).fetchall()
    assert incoming.claim_id is not None
    assert sum(row["status"] == "active" for row in rows) <= 1
    assert {row["conflict_key_version"] for row in rows} == {4}
    assert {row["subject_canonical_entity_id"] for row in rows} == {"person:user"}
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1


def test_ingest_rekeys_v3_explicit_alias_into_same_typed_group(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _store(connection, "user", "seed")
    connection.execute("DELETE FROM claims")
    EntityRepository(connection).create_alias("operator", "person", "person:user", "user_explicit", valid_from=NOW)
    connection.commit()
    existing = _store(connection, "operator", "8080")
    _downgrade_to_v3(connection, str(existing.claim_id))

    _store(connection, "user", "9090")

    rows = connection.execute("SELECT status,conflict_key FROM claims").fetchall()
    assert sum(row["status"] == "active" for row in rows) <= 1
    assert len({row["conflict_key"] for row in rows}) == 1


def test_ingest_fails_closed_when_legacy_rekey_has_no_evidence(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    existing = _store(connection, "user", "8080")
    before = _downgrade_to_v3(connection, str(existing.claim_id))
    connection.execute("DELETE FROM evidence_links WHERE derived_id=?", (existing.claim_id,))
    connection.commit()

    with pytest.raises(EntityCoordinateError, match="evidence proof"):
        _store(connection, "user", "9090")

    after = ClaimRepository(connection).get_claim(str(existing.claim_id))
    assert (after["status"], after["conflict_key"], after["conflict_key_version"]) == (
        before["status"],
        before["conflict_key"],
        3,
    )
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_ingest_fails_closed_when_applicable_v3_rekey_overflows(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    entities = EntityRepository(connection)
    entities.seed_builtins(now=NOW)
    for index in range(17):
        subject = f"operator-{index:02d}"
        entities.create_alias(subject, "person", "person:user", "user_explicit", valid_from=NOW)
        key = compute_conflict_key("default", subject, "configures", "config.port", {"service": "api"}, version=3)
        connection.execute(
            "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,qualifiers_json,"
            "canonical_slot,conflict_key,conflict_key_version,recorded_from,status) "
            "VALUES (?, 'default', ?, 'configures', ?, '{\"service\": \"api\"}', "
            "'config.port', ?, 3, ?, 'active')",
            (f"legacy-{index:02d}", subject, json.dumps(str(8000 + index)), key, NOW),
        )
        connection.execute(
            "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
            "VALUES (?, 'claim', ?, 'event', ?, 'derived_from')",
            (f"proof-{index:02d}", f"legacy-{index:02d}", f"event-{index:02d}"),
        )
    connection.commit()

    with pytest.raises(EntityCoordinateError, match="rekey overflow"):
        _store(connection, "user", "9090")

    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 17


def test_upgraded_schema_no_slot_ingest_skips_legacy_rekey_scan(tmp_path: Path) -> None:
    connection = _upgraded_connection(tmp_path)
    legacy_ids = []
    for index in range(17):
        stored = IngestService.store_extracted(
            connection,
            ExtractedClaim(subject="user", predicate=f"ordinary.note.{index}", value=f"legacy-{index}"),
            {"id": f"event-legacy-{index}", "tenant_id": "default", "actor_type": "user"},
            NOW,
            FakeEmbedder(8),
        )
        legacy_ids.append(str(stored.claim_id))
    connection.execute("DELETE FROM claim_entity_links")
    connection.execute("UPDATE claims SET subject_canonical_entity_id=NULL,conflict_key=NULL,conflict_key_version=3")
    connection.commit()
    before = connection.execute(
        "SELECT id,status,subject_canonical_entity_id,conflict_key,conflict_key_version " "FROM claims ORDER BY id"
    ).fetchall()

    incoming = IngestService.store_extracted(
        connection,
        ExtractedClaim(subject="user", predicate="ordinary.note.new", value="new ordinary claim"),
        {"id": "event-new-ordinary", "tenant_id": "default", "actor_type": "user"},
        NOW,
        FakeEmbedder(8),
    )

    after = connection.execute(
        "SELECT id,status,subject_canonical_entity_id,conflict_key,conflict_key_version "
        "FROM claims WHERE id IN ({}) ORDER BY id".format(",".join("?" for _ in legacy_ids)),
        legacy_ids,
    ).fetchall()
    assert incoming.claim_id is not None
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM claims WHERE conflict_key_version=3").fetchone()[0] == 17


def test_application_rekey_api_does_not_accept_canonical_id_or_conflict_key() -> None:
    parameters = inspect.signature(EntityResolutionService.rekey_claim).parameters
    assert "canonical_entity_id" not in parameters
    assert "conflict_key" not in parameters


def test_application_rekey_stale_status_fingerprint_has_no_projection_write(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    stored = _store(connection, "legacy operator")
    entities = EntityRepository(connection)
    entities.create_alias("legacy operator", "person", "person:user", "user_explicit", valid_from=NOW)
    connection.commit()
    service = EntityResolutionService(connection)
    before = service.claims.get_claim(str(stored.claim_id))
    expected = service.claim_fingerprint(before)
    connection.execute("UPDATE claims SET status='disputed' WHERE id=?", (stored.claim_id,))
    connection.commit()

    assert service.rekey_claim(str(stored.claim_id), expected, changed_at=NOW) == "stale"
    after = service.claims.get_claim(str(stored.claim_id))
    assert after["subject_canonical_entity_id"] is None
    assert after["conflict_key"] == before["conflict_key"]
    assert (
        connection.execute("SELECT count(*) FROM claim_entity_links WHERE claim_id=?", (stored.claim_id,)).fetchone()[0]
        == 0
    )


def test_application_rekey_accepts_equivalent_qualifier_json_whitespace(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    stored = _store(connection, "legacy operator")
    EntityRepository(connection).create_alias(
        "legacy operator", "person", "person:user", "user_explicit", valid_from=NOW
    )
    service = EntityResolutionService(connection)
    before = service.claims.get_claim(str(stored.claim_id))
    connection.execute(
        'UPDATE claims SET qualifiers_json=\'{ "service" : "api" }\' WHERE id=?',
        (stored.claim_id,),
    )
    connection.commit()

    outcome = service.rekey_claim(str(stored.claim_id), service.claim_fingerprint(before), changed_at=NOW)

    assert outcome == "updated"
    after = service.claims.get_claim(str(stored.claim_id))
    assert after["subject_canonical_entity_id"] == "person:user"
    assert after["qualifiers"] == {"service": "api"}


def test_application_rekey_rejects_different_qualifier_json(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    stored = _store(connection, "legacy operator")
    EntityRepository(connection).create_alias(
        "legacy operator", "person", "person:user", "user_explicit", valid_from=NOW
    )
    service = EntityResolutionService(connection)
    before = service.claims.get_claim(str(stored.claim_id))
    connection.execute(
        'UPDATE claims SET qualifiers_json=\'{"service":"web"}\' WHERE id=?',
        (stored.claim_id,),
    )
    connection.commit()

    outcome = service.rekey_claim(str(stored.claim_id), service.claim_fingerprint(before), changed_at=NOW)

    assert outcome == "stale"
    assert service.claims.get_claim(str(stored.claim_id))["subject_canonical_entity_id"] is None


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
    right_before = repository.get_claim(str(right.claim_id))
    service = EntityResolutionService(connection)

    outcome = service.rekey_claim(
        str(right.claim_id),
        service.claim_fingerprint(right_before),
        changed_at=NOW,
    )

    assert outcome == "quarantined"
    assert {row["status"] for row in repository.find_by_conflict_key(left_claim["conflict_key"])} == {"disputed"}
    case = connection.execute("SELECT group_key,rationale FROM conflict_cases").fetchone()
    assert tuple(case) == (left_claim["conflict_key"], "entity_rekey_collision")
    assert service.rekey_claim(str(right.claim_id), service.claim_fingerprint(right_before), changed_at=NOW) == "stale"


def test_rekey_can_join_a_caller_transaction_and_roll_back(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    EntityRepository(connection).seed_builtins(now=NOW)
    connection.commit()
    stored = _store(connection, "legacy operator")
    repository = ClaimRepository(connection)
    before = repository.get_claim(str(stored.claim_id))
    entities = EntityRepository(connection)
    entities.create_alias("legacy operator", "person", "person:user", "user_explicit", valid_from=NOW)
    connection.commit()
    service = EntityResolutionService(connection)
    connection.execute("BEGIN IMMEDIATE")

    outcome = service.rekey_claim(
        str(stored.claim_id),
        service.claim_fingerprint(before),
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
    service = EntityResolutionService(connection)
    connection.execute("BEGIN IMMEDIATE")

    outcome = service.rekey_claim(
        str(right.claim_id),
        service.claim_fingerprint(right_before),
        changed_at=NOW,
        commit=False,
    )
    assert outcome == "quarantined"
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1
    connection.rollback()

    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    assert repository.get_claim(str(left.claim_id)) == left_before
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
    service = EntityResolutionService(connection)

    with pytest.raises(sqlite3.IntegrityError, match="forced entity rekey failure"):
        service.rekey_claim(
            str(right.claim_id),
            service.claim_fingerprint(right_before),
            changed_at=NOW,
        )

    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    assert repository.get_claim(str(left.claim_id)) == left_before
    right_after = repository.get_claim(str(right.claim_id))
    assert right_after["status"] == "active"
    assert right_after["conflict_key"] == right_before["conflict_key"]
    assert right_after["subject_canonical_entity_id"] is None
