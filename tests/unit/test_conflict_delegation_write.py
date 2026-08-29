from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.application.conflicts import ResolutionService
from hl_mem.cli import main
from hl_mem.errors import ConflictResolutionError
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-29T21:00:00+00:00"
LATER = "2026-08-29T22:00:00+00:00"


def _seed_pair(path: Path) -> None:
    connection = Database(path).open()
    repository = ClaimRepository(connection)
    for claim_id, value in (("left", "SQLite"), ("right", "PostgreSQL")):
        assert repository.insert_claim(
            {
                "id": claim_id,
                "namespace_key": "default",
                "subject_entity_id": "project",
                "predicate": "uses",
                "value": value,
                "recorded_from": NOW,
                "status": "disputed",
                "scope": "permanent",
                "source_authority": "medium",
            }
        )
    assert repository.insert_conflict_case(
        {
            "id": "pair-case",
            "pair_key": "left:right",
            "left_claim_id": "left",
            "right_claim_id": "right",
            "status": "manual_required",
            "decision": "uncertain",
            "created_at": NOW,
        }
    )
    connection.close()


def _pair_state(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        "claims": [
            tuple(row)
            for row in connection.execute(
                "SELECT id,status,superseded_by_id,valid_to,recorded_to FROM claims ORDER BY id"
            )
        ],
        "cases": [
            tuple(row)
            for row in connection.execute(
                "SELECT id,status,decision,resolved_at,revision FROM conflict_cases ORDER BY id"
            )
        ],
        "actions": [tuple(row) for row in connection.execute("SELECT * FROM governance_actions ORDER BY id")],
    }


def test_pair_resolution_requires_expected_revision_before_mutation(tmp_path: Path) -> None:
    path = tmp_path / "pair-revision-required.db"
    _seed_pair(path)
    connection = Database(path).open()
    before = _pair_state(connection)

    with pytest.raises(
        ConflictResolutionError,
        match="expected_revision is required for conflict case: pair-case",
    ):
        ResolutionService(connection).resolve("pair-case", "keep_left", resolved_at=NOW)

    assert _pair_state(connection) == before


def test_pair_resolution_rejects_stale_revision_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "pair-stale.db"
    _seed_pair(path)
    connection = Database(path).open()
    before = _pair_state(connection)

    with pytest.raises(
        ConflictResolutionError,
        match="stale conflict revision: expected 7, current 0",
    ):
        ResolutionService(connection).resolve(
            "pair-case",
            "keep_left",
            resolved_at=NOW,
            expected_revision=7,
        )

    assert _pair_state(connection) == before


def test_pair_human_resolution_persists_governance_action_structure(tmp_path: Path) -> None:
    path = tmp_path / "pair-audit.db"
    _seed_pair(path)
    connection = Database(path).open()

    result = ResolutionService(connection).resolve(
        "pair-case",
        "keep_left",
        resolved_at=NOW,
        rationale="verified current database",
        expected_revision=0,
        resolver="agent:test-runner",
    )

    row = connection.execute("SELECT * FROM governance_actions").fetchone()
    assert row is not None
    assert {
        "domain": row["domain"],
        "subject_ref": row["subject_ref"],
        "policy_version": row["policy_version"],
        "tier": row["tier"],
        "decision": row["decision"],
        "confidence": row["confidence"],
        "resolution_rule": row["resolution_rule"],
        "resolver_model": row["resolver_model"],
        "evidence_ids": json.loads(row["evidence_ids_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "applied_at": row["applied_at"],
    } == {
        "domain": "conflict",
        "subject_ref": "pair-case",
        "policy_version": "conflict-human-resolution-v1",
        "tier": "human",
        "decision": "keep_left",
        "confidence": None,
        "resolution_rule": "verified current database",
        "resolver_model": "agent:test-runner",
        "evidence_ids": [],
        "status": "applied",
        "created_at": NOW,
        "applied_at": NOW,
    }
    before = json.loads(row["before_json"])
    after = json.loads(row["after_json"])
    assert before == {
        "candidate_key": None,
        "case_id": "pair-case",
        "case_status": "manual_required",
        "decision": "keep_left",
        "rationale": "verified current database",
        "resolver": "agent:test-runner",
        "revision": 0,
    }
    assert after == {
        **before,
        "case_status": "resolved",
        "revision": connection.execute("SELECT revision FROM conflict_cases WHERE id='pair-case'").fetchone()[0],
    }
    assert after["revision"] > before["revision"]
    assert result["status"] == "resolved"


def test_terminal_pair_replay_uses_action_time_for_new_audit_row(tmp_path: Path) -> None:
    path = tmp_path / "pair-terminal-replay.db"
    _seed_pair(path)
    connection = Database(path).open()
    service = ResolutionService(connection)
    first = service.resolve(
        "pair-case",
        "keep_left",
        resolved_at=NOW,
        expected_revision=0,
    )
    current_revision = connection.execute("SELECT revision FROM conflict_cases WHERE id='pair-case'").fetchone()[0]

    replay = service.resolve(
        "pair-case",
        "keep_left",
        resolved_at=LATER,
        rationale="audited legacy replay",
        expected_revision=current_revision,
        resolver="agent:replay",
    )

    row = connection.execute("SELECT created_at,applied_at FROM governance_actions").fetchone()
    assert row is not None
    assert (row["created_at"], row["applied_at"]) == (LATER, LATER)
    assert replay["resolved_at"] == first["resolved_at"] == NOW


def test_cli_resolve_accepts_custom_resolver_and_persists_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cli-resolver.db"
    _seed_pair(path)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    main(
        [
            "--db",
            str(path),
            "conflicts",
            "resolve",
            "pair-case",
            "keep_left",
            "--expected-revision",
            "0",
            "--resolver",
            "agent:custom",
        ]
    )

    connection = Database(path).open()
    row = connection.execute("SELECT resolver_model FROM governance_actions").fetchone()
    assert row is not None
    assert row["resolver_model"] == "agent:custom"


def test_cli_resolve_requires_expected_revision_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cli-revision-required.db"
    _seed_pair(path)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings.for_test())

    with pytest.raises(SystemExit, match="2"):
        main(["--db", str(path), "conflicts", "resolve", "pair-case", "keep_left"])

    connection = Database(path).open()
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='pair-case'").fetchone()[0] == (
        "manual_required"
    )
    assert connection.execute("SELECT count(*) FROM governance_actions").fetchone()[0] == 0
