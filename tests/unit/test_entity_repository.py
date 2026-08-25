from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.domain.entity_coordinates import AmbiguousEntityAliasError, EntityTypeMismatchError
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
        "SELECT canonical_entity_id,version,valid_to FROM entity_aliases "
        "WHERE alias_normalized=? ORDER BY version",
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
    alias = repository.create_alias(
        "本地小马", "agent", "agent:local_pony", "user_explicit", valid_from=NOW
    )

    with pytest.raises(UnknownCanonicalEntityError):
        repository.create_alias("小马", "agent", alias["id"], "user_explicit", valid_from=NOW)
    with pytest.raises(UnknownCanonicalEntityError):
        repository.create_alias("循环", "agent", "循环", "user_explicit", valid_from=NOW)


def test_entity_alias_relation_and_claim_link_enforce_typed_foreign_keys(tmp_path: Path) -> None:
    repository, connection = _repository(tmp_path)
    repository.create_entity("agent:local_pony", "agent", "local_pony", "本地小马", now=NOW)
    repository.create_entity("device:user_local_pc", "device", "user_local_pc", "用户本地电脑", now=NOW)
    repository.create_entity("environment:local_runtime", "environment", "local_runtime", "本地环境", now=NOW)
    repository.create_entity("topic:memory", "topic", "memory", "Memory", now=NOW)

    with pytest.raises(EntityTypeMismatchError):
        repository.create_alias(
            "本地小马", "environment", "agent:local_pony", "user_explicit", valid_from=NOW
        )
    relation = repository.create_relation(
        "agent:local_pony",
        "device:user_local_pc",
        "runs_on",
        confidence=0.95,
        valid_from=NOW,
    )
    assert relation["relation"] == "runs_on"

    _insert_claim(connection)
    link = repository.link_claim(
        "claim-1",
        "environment:local_runtime",
        "environment",
        mention_text="本地环境",
        resolution_confidence=1.0,
        alias_version=1,
        proof_id=None,
    )
    assert link["role"] == "environment"
    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "claim-1",
            "agent:local_pony",
            "environment",
            mention_text="本地小马",
            resolution_confidence=1.0,
        )
    with pytest.raises(EntityTypeMismatchError):
        repository.link_claim(
            "claim-1",
            "topic:memory",
            "subject",
            mention_text="memory",
            resolution_confidence=1.0,
        )


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
