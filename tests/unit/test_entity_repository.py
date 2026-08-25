from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.domain.entity_coordinates import (
    AmbiguousEntityAliasError,
    EntityCoordinateError,
    EntityTypeMismatchError,
)
from hl_mem.storage.database import Database
from hl_mem.storage.entities import EntityRepository, UnknownCanonicalEntityError

NOW = "2026-08-25T10:00:00+00:00"
LATER = "2026-08-25T11:00:00+00:00"


def _repository(tmp_path: Path) -> tuple[EntityRepository, sqlite3.Connection]:
    connection = Database(tmp_path / "entities.db").open()
    return EntityRepository(connection), connection


def _insert_claim(connection: sqlite3.Connection, claim_id: str = "claim-1") -> None:
    connection.execute(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,?,?,'active')",
        (claim_id, '"value"', NOW),
    )


def _insert_event(connection: sqlite3.Connection, event_id: str, tenant_id: str = "default") -> None:
    connection.execute(
        "INSERT INTO events("
        "id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at"
        ") VALUES (?,?, 'message','user','{}',?,?)",
        (event_id, tenant_id, NOW, NOW),
    )


def _insert_proof(
    connection: sqlite3.Connection,
    proof_id: str,
    claim_id: str,
    *,
    evidence_id: str = "event-proof",
) -> None:
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES (?,'claim',?,'event',?,'supports')",
        (proof_id, claim_id, evidence_id),
    )


def test_builtin_seeding_is_idempotent_and_uses_the_resolver_path(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)

    first = repository.seed_builtins("default", now=NOW)
    second = repository.seed_builtins("default", now=LATER)

    assert first[0] >= 5
    assert first[1] >= 23
    assert second == (0, 0)
    assert connection.execute("SELECT count(*) FROM canonical_entities").fetchone()[0] == first[0]
    assert repository.resolve_alias("本地小马").canonical_entity_id == "agent:local_pony"
    assert repository.resolve_alias("用户本地电脑").canonical_entity_id == "device:user_local_pc"
    assert repository.resolve_alias("本地环境").canonical_entity_id == "environment:local_runtime"
    assert repository.resolve_alias("HL-Mem", expected_type="project").canonical_entity_id == "project:hl_mem"
    assert repository.resolve_alias("我", expected_type="person").canonical_entity_id == "person:user"


def test_builtin_seeding_preserves_exact_ids_in_each_namespace(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)

    first = repository.seed_builtins("tenant-a", now=NOW)
    second = repository.seed_builtins("tenant-b", now=NOW)

    assert first == second
    assert connection.execute("SELECT count(*) FROM canonical_entities WHERE id='person:user'").fetchone()[0] == 2
    assert repository.resolve_alias("我", namespace_key="tenant-a").canonical_entity_id == "person:user"
    assert repository.resolve_alias("我", namespace_key="tenant-b").canonical_entity_id == "person:user"


def test_builtin_seeding_preserves_an_existing_explicit_alias_to_the_same_target(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("person:user", "person", "user", "User", now=NOW)
    repository.create_alias("我", "person", "person:user", "user_explicit", valid_from=NOW)

    repository.seed_builtins("default", now=LATER)

    row = connection.execute(
        "SELECT source_kind,version FROM entity_aliases WHERE alias_normalized='我' AND valid_to IS NULL"
    ).fetchone()
    assert tuple(row) == ("user_explicit", 1)


def test_repository_resolves_only_unique_active_alias_with_optional_type(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_entity("agent:shared", "agent", "shared", "Shared Agent", now=NOW)
    repository.create_entity("environment:shared", "environment", "shared", "Shared Environment", now=NOW)
    repository.create_alias("shared", "agent", "agent:shared", "user_explicit", valid_from=NOW)
    repository.create_alias("shared", "environment", "environment:shared", "user_explicit", valid_from=NOW)

    with pytest.raises(AmbiguousEntityAliasError):
        repository.resolve_alias("  ＳＨＡＲＥＤ ")
    assert repository.resolve_alias("shared", expected_type="agent").canonical_entity_id == "agent:shared"
    with pytest.raises(EntityTypeMismatchError):
        repository.resolve_alias("shared", expected_type="person")


def test_alias_close_then_create_progresses_version_without_rewriting_history(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:first", "agent", "first", "First", now=NOW)
    repository.create_entity("agent:second", "agent", "second", "Second", now=NOW)
    first = repository.create_alias("小马", "agent", "agent:first", "user_explicit", valid_from=NOW)

    assert repository.close_alias("小马", "agent", valid_to=LATER) == 1
    second = repository.create_alias("小马", "agent", "agent:second", "user_explicit", valid_from=LATER)

    rows = connection.execute(
        "SELECT canonical_entity_id,version,valid_to FROM entity_aliases " "WHERE alias_normalized=? ORDER BY version",
        ("小马",),
    ).fetchall()
    assert first["version"] == 1
    assert second["version"] == 2
    assert [tuple(row) for row in rows] == [
        ("agent:first", 1, LATER),
        ("agent:second", 2, None),
    ]


def test_alias_chain_and_cycle_targets_are_rejected(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    repository.create_entity("agent:local_pony", "agent", "local_pony", "本地小马", now=NOW)
    alias = repository.create_alias("本地小马", "agent", "agent:local_pony", "user_explicit", valid_from=NOW)

    with pytest.raises(UnknownCanonicalEntityError):
        repository.create_alias("小马", "agent", alias["id"], "user_explicit", valid_from=NOW)
    with pytest.raises(UnknownCanonicalEntityError):
        repository.create_alias("循环", "agent", "循环", "user_explicit", valid_from=NOW)


def test_malformed_raw_alias_candidate_fails_closed_in_repository_lookup(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO canonical_entities("
        "id,namespace_key,entity_type,canonical_key,display_name,status,created_at,updated_at"
        ") VALUES ('agent:e_short','default','agent','e_short','Bad','active',?,?)",
        (NOW, NOW),
    )
    connection.execute(
        "INSERT INTO entity_aliases("
        "id,namespace_key,alias_normalized,entity_type,canonical_entity_id,version,source_kind,"
        "valid_from,created_at"
        ") VALUES ('bad-alias','default','bad','agent','agent:e_short',1,'migration_exact',?,?)",
        (NOW, NOW),
    )
    connection.execute("PRAGMA ignore_check_constraints=OFF")

    assert repository.resolve_alias("bad") is None


def test_alias_and_relation_source_events_must_share_namespace(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:local_pony", "agent", "local_pony", "本地小马", namespace_key="tenant-a", now=NOW)
    repository.create_entity(
        "device:user_local_pc",
        "device",
        "user_local_pc",
        "用户本地电脑",
        namespace_key="tenant-a",
        now=NOW,
    )
    _insert_event(connection, "event-b", "tenant-b")

    with pytest.raises(EntityTypeMismatchError):
        repository.create_alias(
            "本地小马",
            "agent",
            "agent:local_pony",
            "user_explicit",
            namespace_key="tenant-a",
            source_event_id="event-b",
            valid_from=NOW,
        )
    with pytest.raises(EntityTypeMismatchError):
        repository.create_relation(
            "agent:local_pony",
            "device:user_local_pc",
            "runs_on",
            namespace_key="tenant-a",
            source_event_id="event-b",
            confidence=1.0,
            valid_from=NOW,
        )


def test_entity_alias_relation_and_claim_link_enforce_typed_foreign_keys(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:local_pony", "agent", "local_pony", "本地小马", now=NOW)
    repository.create_entity("device:user_local_pc", "device", "user_local_pc", "用户本地电脑", now=NOW)
    repository.create_entity("environment:local_runtime", "environment", "local_runtime", "本地环境", now=NOW)
    repository.create_entity("topic:memory", "topic", "memory", "Memory", now=NOW)
    repository.create_alias("本地环境", "environment", "environment:local_runtime", "builtin", valid_from=NOW)

    with pytest.raises(EntityTypeMismatchError):
        repository.create_alias("本地小马", "environment", "agent:local_pony", "user_explicit", valid_from=NOW)
    relation = repository.create_relation(
        "agent:local_pony",
        "device:user_local_pc",
        "runs_on",
        confidence=0.95,
        valid_from=NOW,
    )
    assert relation["relation"] == "runs_on"

    _insert_claim(connection)
    _insert_proof(connection, "proof-1", "claim-1")
    link = repository.link_claim(
        "claim-1",
        "environment:local_runtime",
        "environment",
        mention_text="本地环境",
        resolution_confidence=1.0,
        alias_version=1,
        proof_id="proof-1",
    )
    assert link["role"] == "environment"
    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "claim-1",
            "agent:local_pony",
            "environment",
            mention_text="本地小马",
            resolution_confidence=1.0,
            alias_version=1,
            proof_id="proof-1",
        )
    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "claim-1",
            "topic:memory",
            "subject",
            mention_text="memory",
            resolution_confidence=1.0,
            alias_version=1,
            proof_id="proof-1",
        )


def test_claim_link_requires_matching_alias_and_claim_evidence_proof(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:local_pony", "agent", "local_pony", "本地小马", now=NOW)
    repository.create_alias("pony", "agent", "agent:local_pony", "user_explicit", valid_from=NOW)
    repository.create_alias("equine", "agent", "agent:local_pony", "user_explicit", valid_from=NOW)
    for claim_id in ("claim-a", "claim-b", "claim-c"):
        _insert_claim(connection, claim_id)
    _insert_proof(connection, "proof-a", "claim-a")
    _insert_proof(connection, "proof-b", "claim-b")
    _insert_proof(connection, "proof-c", "claim-c")

    stored = repository.link_claim(
        "claim-a",
        "agent:local_pony",
        "actor",
        mention_text=" ＰＯＮＹ ",
        resolution_confidence=0.9,
        alias_version=1,
        proof_id="proof-a",
    )
    assert stored["mention_text"] == "pony"

    invalid_payloads = (
        {"claim_id": "claim-b", "mention_text": "horse", "alias_version": 1, "proof_id": "proof-b"},
        {"claim_id": "claim-b", "mention_text": "pony", "alias_version": 9, "proof_id": "proof-b"},
        {"claim_id": "claim-b", "mention_text": "pony", "alias_version": 1, "proof_id": "proof-a"},
        {"claim_id": "claim-c", "mention_text": "pony", "alias_version": None, "proof_id": "proof-c"},
        {"claim_id": "claim-c", "mention_text": "pony", "alias_version": 1, "proof_id": None},
    )
    for payload in invalid_payloads:
        with pytest.raises(EntityTypeMismatchError):
            repository.link_claim(
                payload["claim_id"],
                "agent:local_pony",
                "actor",
                mention_text=payload["mention_text"],
                resolution_confidence=1.0,
                alias_version=payload["alias_version"],
                proof_id=payload["proof_id"],
            )


def test_claim_link_idempotent_replay_rejects_any_payload_mismatch(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:local_pony", "agent", "local_pony", "本地小马", now=NOW)
    repository.create_alias("pony", "agent", "agent:local_pony", "user_explicit", valid_from=NOW)
    _insert_claim(connection)
    _insert_proof(connection, "proof-1", "claim-1")
    _insert_proof(connection, "proof-2", "claim-1", evidence_id="event-proof-2")
    base = {
        "mention_text": "pony",
        "resolution_confidence": 0.9,
        "alias_version": 1,
        "proof_id": "proof-1",
    }
    first = repository.link_claim("claim-1", "agent:local_pony", "actor", **base)
    assert repository.link_claim("claim-1", "agent:local_pony", "actor", **base) == first

    repository.close_alias("pony", "agent", valid_to=LATER)
    repository.create_alias("pony", "agent", "agent:local_pony", "user_explicit", valid_from=LATER)

    for field, value in (
        ("mention_text", "equine"),
        ("resolution_confidence", 0.8),
        ("alias_version", 2),
        ("proof_id", "proof-2"),
    ):
        mismatched = {**base, field: value}
        with pytest.raises(EntityCoordinateError):
            repository.link_claim("claim-1", "agent:local_pony", "actor", **mismatched)


def test_repository_rejects_cross_namespace_claim_link(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity(
        "environment:other_runtime",
        "environment",
        "other_runtime",
        "Other Runtime",
        namespace_key="other",
        now=NOW,
    )
    _insert_claim(connection)

    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "claim-1",
            "environment:other_runtime",
            "environment",
            mention_text="runtime",
            resolution_confidence=1.0,
        )


def test_repository_does_not_commit_callers_existing_transaction(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    connection.execute("BEGIN IMMEDIATE")

    repository.create_entity("agent:rollback", "agent", "rollback", "Rollback", now=NOW)
    repository.create_alias("rollback", "agent", "agent:rollback", "user_explicit", valid_from=NOW)
    connection.rollback()

    assert connection.execute("SELECT 1 FROM canonical_entities WHERE id='agent:rollback'").fetchone() is None
    assert connection.execute("SELECT 1 FROM entity_aliases WHERE alias_normalized='rollback'").fetchone() is None


def test_claim_link_rejects_closed_alias_and_retired_target(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:pony", "agent", "pony", "Pony", now=NOW)
    repository.create_alias("pony", "agent", "agent:pony", "user_explicit", valid_from=NOW)
    for claim_id in ("closed", "retired"):
        _insert_claim(connection, claim_id)
        _insert_proof(connection, f"proof-{claim_id}", claim_id)

    repository.close_alias("pony", "agent", valid_to=LATER)
    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "closed",
            "agent:pony",
            "subject",
            mention_text="pony",
            resolution_confidence=1.0,
            alias_version=1,
            proof_id="proof-closed",
        )
    repository.create_alias("pony", "agent", "agent:pony", "user_explicit", valid_from=LATER)
    connection.execute("UPDATE canonical_entities SET status='retired' WHERE id='agent:pony'")
    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "retired",
            "agent:pony",
            "subject",
            mention_text="pony",
            resolution_confidence=1.0,
            alias_version=2,
            proof_id="proof-retired",
        )


def test_raw_claim_link_insert_rejects_closed_alias(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:pony", "agent", "pony", "Pony", now=NOW)
    repository.create_alias("pony", "agent", "agent:pony", "user_explicit", valid_from=NOW)
    repository.close_alias("pony", "agent", valid_to=LATER)
    _insert_claim(connection)
    _insert_proof(connection, "proof-1", "claim-1")

    with pytest.raises(sqlite3.IntegrityError, match="alias or evidence proof mismatch"):
        connection.execute(
            "INSERT INTO claim_entity_links VALUES " "('claim-1','agent:pony','subject','pony',1.0,1,'proof-1')"
        )
