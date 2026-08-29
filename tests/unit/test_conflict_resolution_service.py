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


def _orphan_disputed_ids(connection) -> list[str]:
    rows = connection.execute(
        "SELECT claims.id FROM claims WHERE claims.status='disputed' AND NOT EXISTS ("
        "SELECT 1 FROM conflict_cases AS cases "
        "WHERE (cases.left_claim_id=claims.id OR cases.right_claim_id=claims.id) "
        "AND cases.status IN ('pending','auto_resolved','manual_required')"
        ") ORDER BY claims.id"
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _revision(connection: sqlite3.Connection, case_id: str) -> int:
    return int(connection.execute("SELECT revision FROM conflict_cases WHERE id=?", (case_id,)).fetchone()[0])


def test_exclusive_group_rejects_coexist_with_executable_constraint(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    with pytest.raises(
        ConflictResolutionError,
        match="该组必须选择唯一有效候选；当前接口不提供在线坐标修正",
    ):
        ResolutionService(connection).resolve(
            "case-left-right",
            "coexist",
            resolved_at=NOW,
            expected_revision=_revision(connection, "case-left-right"),
        )

    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right", "third")} == {
        "candidate",
        "disputed",
    }
    assert {row["status"] for row in connection.execute("SELECT status FROM conflict_cases")} == {"manual_required"}


def test_exclusive_group_rejects_reject_with_executable_constraint(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    with pytest.raises(
        ConflictResolutionError,
        match="该组必须选择唯一有效候选；当前接口不提供在线坐标修正",
    ):
        ResolutionService(connection).resolve(
            "case-left-right",
            "reject",
            resolved_at=NOW,
            expected_revision=_revision(connection, "case-left-right"),
        )

    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right", "third")} == {
        "candidate",
        "disputed",
    }
    assert {row["status"] for row in connection.execute("SELECT status FROM conflict_cases")} == {"manual_required"}


def test_keep_left_converges_group_and_closes_every_overlapping_open_case(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    result = ResolutionService(connection).resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        expected_revision=_revision(connection, "case-left-right"),
    )

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


def test_keep_left_propagates_rationale_to_every_closed_group_case(tmp_path) -> None:
    connection, _ = _exclusive_group(tmp_path)

    ResolutionService(connection).resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        rationale="人工核对配置记录后保留 8080",
        expected_revision=_revision(connection, "case-left-right"),
    )

    rows = connection.execute("SELECT id,rationale FROM conflict_cases ORDER BY id").fetchall()
    assert {row["rationale"] for row in rows} == {"人工核对配置记录后保留 8080"}


def test_resolution_without_rationale_preserves_each_open_case_value(tmp_path) -> None:
    connection, _ = _exclusive_group(tmp_path)
    connection.execute("UPDATE conflict_cases SET rationale='left-right evidence' WHERE id='case-left-right'")
    connection.execute("UPDATE conflict_cases SET rationale='left-third evidence' WHERE id='case-left-third'")
    connection.execute("UPDATE conflict_cases SET rationale='right-third evidence' WHERE id='case-right-third'")
    connection.commit()

    ResolutionService(connection).resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        expected_revision=_revision(connection, "case-left-right"),
    )

    rows = connection.execute("SELECT id,rationale FROM conflict_cases ORDER BY id").fetchall()
    assert {row["id"]: row["rationale"] for row in rows} == {
        "case-left-right": "left-right evidence",
        "case-left-third": "left-third evidence",
        "case-right-third": "right-third evidence",
    }


def test_terminal_replay_without_rationale_preserves_group_values(tmp_path) -> None:
    connection, _ = _exclusive_group(tmp_path)
    service = ResolutionService(connection)
    service.resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        rationale="original rationale",
        expected_revision=_revision(connection, "case-left-right"),
    )

    service.resolve(
        "case-left-right",
        "keep_left",
        resolved_at="later",
        expected_revision=_revision(connection, "case-left-right"),
    )

    rows = connection.execute("SELECT rationale FROM conflict_cases").fetchall()
    assert {row["rationale"] for row in rows} == {"original rationale"}


def test_terminal_replay_with_matching_decision_replaces_group_rationale(tmp_path) -> None:
    connection, _ = _exclusive_group(tmp_path)
    service = ResolutionService(connection)
    service.resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        rationale="original rationale",
        expected_revision=_revision(connection, "case-left-right"),
    )

    service.resolve(
        "case-left-right",
        "keep_left",
        resolved_at="later",
        rationale="reviewed rationale",
        expected_revision=_revision(connection, "case-left-right"),
    )

    rows = connection.execute("SELECT rationale FROM conflict_cases").fetchall()
    assert {row["rationale"] for row in rows} == {"reviewed rationale"}


def test_terminal_replay_with_different_decision_does_not_replace_rationale(tmp_path) -> None:
    connection, _ = _exclusive_group(tmp_path)
    service = ResolutionService(connection)
    service.resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        rationale="original rationale",
        expected_revision=_revision(connection, "case-left-right"),
    )

    with pytest.raises(ConflictResolutionError, match="different decision"):
        service.resolve(
            "case-left-right",
            "keep_right",
            resolved_at="later",
            rationale="must not be stored",
            expected_revision=_revision(connection, "case-left-right"),
        )

    rows = connection.execute("SELECT rationale FROM conflict_cases").fetchall()
    assert {row["rationale"] for row in rows} == {"original rationale"}


@pytest.mark.parametrize("retry_decision", ["keep_left", "keep_right"])
def test_group_winner_case_retry_returns_established_winner(tmp_path, retry_decision) -> None:
    connection, _ = _exclusive_group(tmp_path)
    service = ResolutionService(connection)
    service.resolve(
        "case-left-right",
        "keep_left",
        resolved_at=NOW,
        expected_revision=_revision(connection, "case-left-right"),
    )

    result = service.resolve(
        "case-right-third",
        retry_decision,
        resolved_at="later",
        expected_revision=_revision(connection, "case-right-third"),
    )

    assert result["status"] == "resolved"
    assert result["winner_id"] == "left"
    assert result["resolved_at"] == NOW
    assert result["closed_case_ids"] == ["case-right-third"]


def test_group_resolution_rolls_back_when_overlapping_case_close_fails(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)
    connection.execute(
        "CREATE TRIGGER fail_second_case BEFORE UPDATE ON conflict_cases "
        "WHEN old.id='case-left-third' BEGIN SELECT RAISE(ABORT,'injected close failure'); END"
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected close failure"):
        ResolutionService(connection).resolve(
            "case-left-right",
            "keep_left",
            resolved_at=NOW,
            rationale="must roll back with the group",
            expected_revision=_revision(connection, "case-left-right"),
        )

    assert {claim_id: repository.get_claim(claim_id)["status"] for claim_id in ("left", "right", "third")} == {
        "left": "disputed",
        "right": "disputed",
        "third": "candidate",
    }
    assert {row["status"] for row in connection.execute("SELECT status FROM conflict_cases")} == {"manual_required"}
    assert {row["rationale"] for row in connection.execute("SELECT rationale FROM conflict_cases")} == {None}


def test_nonexclusive_pair_can_coexist(tmp_path) -> None:
    connection = Database(tmp_path / "coexist.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", value="alpha", slot=None, conflict_key=None)
    _claim(repository, "right", value="beta", slot=None, conflict_key=None)
    _case(repository, "case", "left", "right")

    result = ResolutionService(connection).resolve(
        "case",
        "coexist",
        resolved_at=NOW,
        expected_revision=_revision(connection, "case"),
    )

    assert result["decision"] == "coexist"
    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right")} == {"active"}
    case = connection.execute("SELECT status,decision,resolved_at FROM conflict_cases").fetchone()
    assert tuple(case) == ("resolved", "coexist", NOW)


def test_nonexclusive_reject_restores_both_claims_and_leaves_no_orphans(tmp_path) -> None:
    connection = Database(tmp_path / "reject.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", value="alpha", slot=None, conflict_key=None)
    _claim(repository, "right", value="beta", slot=None, conflict_key=None)
    _case(repository, "case", "left", "right")

    result = ResolutionService(connection).resolve(
        "case",
        "reject",
        resolved_at=NOW,
        expected_revision=_revision(connection, "case"),
    )

    assert result["status"] == "rejected"
    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right")} == {"active"}
    assert _orphan_disputed_ids(connection) == []
    case = connection.execute("SELECT status,decision,resolved_at FROM conflict_cases").fetchone()
    assert tuple(case) == ("rejected", "reject", NOW)


def test_reject_orphan_postcondition_violation_rolls_back(tmp_path) -> None:
    connection = Database(tmp_path / "reject-orphan.db").open()
    repository = ClaimRepository(connection)
    _claim(repository, "left", value="alpha", slot=None, conflict_key=None)
    _claim(repository, "right", value="beta", slot=None, conflict_key=None)
    _claim(repository, "unrelated-orphan", value="gamma", slot=None, conflict_key=None)
    _case(repository, "case", "left", "right")

    with pytest.raises(ConflictResolutionError, match="orphan disputed claim: unrelated-orphan"):
        ResolutionService(connection).resolve(
            "case",
            "reject",
            resolved_at=NOW,
            expected_revision=_revision(connection, "case"),
        )

    assert {repository.get_claim(claim_id)["status"] for claim_id in ("left", "right")} == {"disputed"}
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case'").fetchone()[0] == "manual_required"


def test_resolution_rejects_unknown_decision_without_mutation(tmp_path) -> None:
    connection, repository = _exclusive_group(tmp_path)

    with pytest.raises(ConflictResolutionError, match="unsupported conflict decision"):
        ResolutionService(connection).resolve(
            "case-left-right",
            "invalid",
            resolved_at=NOW,
            rationale="must not be stored",
        )

    assert repository.get_claim("left")["status"] == "disputed"
    assert (
        connection.execute("SELECT status FROM conflict_cases WHERE id='case-left-right'").fetchone()[0]
        == "manual_required"
    )
    assert connection.execute("SELECT rationale FROM conflict_cases WHERE id='case-left-right'").fetchone()[0] is None
