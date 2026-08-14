from __future__ import annotations

import sqlite3

import pytest

from hl_mem.application.conflicts import ResolutionService
from hl_mem.errors import ConflictResolutionError
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-15T08:00:00+00:00"


def _claim(
    repository: ClaimRepository,
    claim_id: str,
    *,
    value: str,
    status: str = "disputed",
    slot: str | None = "config.port",
    conflict_key: str | None = "port-group",
) -> None:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "gateway",
            "predicate": "配置",
            "value": value,
            "qualifiers": {"service": "gateway"} if slot == "config.port" else {},
            "canonical_attribute": slot or "fact.other",
            "canonical_slot": slot,
            "fact_hash": f"hash-{claim_id}",
            "conflict_key": conflict_key,
            "conflict_key_version": 3,
            "valid_from": NOW,
            "recorded_from": NOW,
            "observed_at": NOW,
            "status": status,
            "confidence": 0.9,
            "importance": 0.5,
            "scope": "permanent",
            "volatility": "stable",
            "source_authority": "medium",
        }
    )


def _case(repository: ClaimRepository, case_id: str, left_id: str, right_id: str) -> None:
    assert repository.insert_conflict_case(
        {
            "id": case_id,
            "pair_key": f"pair-{case_id}",
            "left_claim_id": left_id,
            "right_claim_id": right_id,
            "status": "manual_required",
            "decision": "uncertain",
            "created_at": NOW,
        }
    )


def _exclusive_group(tmp_path):
    connection = Database(tmp_path / "resolution.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", value="8080")
    _claim(repository, "right", value="8081")
    _claim(repository, "third", value="9090", status="candidate")
    _case(repository, "case-left-right", "left", "right")
    _case(repository, "case-left-third", "left", "third")
    _case(repository, "case-right-third", "right", "third")
    return connection, repository


def test_exclusive_group_rejects_coexist_with_remediation_reason(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    with pytest.raises(
        ConflictResolutionError,
        match="应共存需先修正 slot/qualifier 使脱离同 conflict key",
    ):
        ResolutionService(connection).resolve("case-left-right", "coexist", resolved_at=NOW)

    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right", "third")} == {
        "candidate",
        "disputed",
    }
    assert {row["status"] for row in connection.execute("SELECT status FROM conflict_cases")} == {"manual_required"}


def test_keep_left_converges_group_and_closes_every_overlapping_open_case(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    result = ResolutionService(connection).resolve("case-left-right", "keep_left", resolved_at=NOW)

    assert result["winner_id"] == "left"
    assert result["closed_case_ids"] == [
        "case-left-right",
        "case-left-third",
        "case-right-third",
    ]
    assert repository.get_claim("left")["status"] == "active"
    for loser_id in ("right", "third"):
        loser = repository.get_claim(loser_id)
        assert (loser["status"], loser["superseded_by_id"]) == ("superseded", "left")
    rows = connection.execute("SELECT id,status,decision,resolved_at FROM conflict_cases ORDER BY id").fetchall()
    assert {row["status"] for row in rows} == {"resolved"}
    assert {row["resolved_at"] for row in rows} == {NOW}
    assert {row["decision"] for row in rows} == {"keep_left", "group_winner"}
    active_count = connection.execute(
        "SELECT count(*) FROM claims WHERE conflict_key='port-group' AND status='active'"
    ).fetchone()[0]
    assert active_count == 1


def test_group_resolution_rolls_back_when_overlapping_case_close_fails(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)
    connection.execute(
        "CREATE TRIGGER fail_second_case BEFORE UPDATE ON conflict_cases "
        "WHEN old.id='case-left-third' BEGIN SELECT RAISE(ABORT,'injected close failure'); END"
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected close failure"):
        ResolutionService(connection).resolve("case-left-right", "keep_left", resolved_at=NOW)

    assert {claim_id: repository.get_claim(claim_id)["status"] for claim_id in ("left", "right", "third")} == {
        "left": "disputed",
        "right": "disputed",
        "third": "candidate",
    }
    assert {row["status"] for row in connection.execute("SELECT status FROM conflict_cases")} == {"manual_required"}


def test_nonexclusive_pair_can_coexist(tmp_path) -> None:
    connection = Database(tmp_path / "coexist.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", value="alpha", slot=None, conflict_key=None)
    _claim(repository, "right", value="beta", slot=None, conflict_key=None)
    _case(repository, "case", "left", "right")

    result = ResolutionService(connection).resolve("case", "coexist", resolved_at=NOW)

    assert result["decision"] == "coexist"
    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right")} == {"active"}
    case = connection.execute("SELECT status,decision,resolved_at FROM conflict_cases").fetchone()
    assert tuple(case) == ("resolved", "coexist", NOW)


def test_resolution_rejects_unknown_decision_without_mutation(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    with pytest.raises(ConflictResolutionError, match="unsupported conflict decision"):
        ResolutionService(connection).resolve("case-left-right", "invalid", resolved_at=NOW)

    assert repository.get_claim("left")["status"] == "disputed"
    assert (
        connection.execute("SELECT status FROM conflict_cases WHERE id='case-left-right'").fetchone()[0]
        == "manual_required"
    )
