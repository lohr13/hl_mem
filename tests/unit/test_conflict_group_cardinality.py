from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from hl_mem.errors import ConflictError
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.consolidate import auto_resolve_conflicts

NOW = "2026-08-18T08:00:00+00:00"


def _claim(
    repository: ClaimRepository,
    claim_id: str,
    value: str,
    *,
    namespace: str = "default",
    slot: str = "config.port",
    conflict_key: str = "gateway-port",
) -> dict[str, object]:
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": namespace,
            "subject_entity_id": "gateway",
            "predicate": "配置",
            "value": value,
            "qualifiers": {"service": "gateway"} if slot == "config.port" else {"purpose": "runtime"},
            "canonical_attribute": slot,
            "canonical_slot": slot,
            "fact_hash": f"hash-{claim_id}",
            "conflict_key": conflict_key,
            "conflict_key_version": 3,
            "recorded_from": NOW,
            "status": "disputed",
            "source_authority": "medium",
            "scope": "permanent",
            "volatility": "stable",
        }
    )
    stored = repository.get_claim(claim_id)
    assert stored is not None
    return stored


def _ensure(repository: ClaimRepository, members: list[dict[str, object]]) -> dict[str, object]:
    return repository.ensure_group_conflict_case(
        members,
        created_at=NOW,
        decision="uncertain",
        rationale="test_group",
    )


def test_101_member_exclusive_group_persists_one_case_not_all_pairs(tmp_path: Path) -> None:
    connection = Database(tmp_path / "group-101.db").open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index:03d}", str(8_000 + index)) for index in range(101)]

    result = _ensure(repository, members)

    assert result["outcome"] == "created"
    assert connection.execute(
        "SELECT count(*) FROM conflict_cases WHERE status IN ('pending','auto_resolved','manual_required')"
    ).fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM conflict_case_candidates").fetchone()[0] == 101
    assert connection.execute("SELECT count(*) FROM conflict_candidate_members").fetchone()[0] == 101
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] != 101 * 100 // 2


def test_102nd_candidate_attaches_to_same_case_and_bumps_revision_once(tmp_path: Path) -> None:
    connection = Database(tmp_path / "group-102.db").open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index:03d}", str(8_000 + index)) for index in range(101)]
    first = _ensure(repository, members)
    connection.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='test_clean' WHERE case_id=?",
        (first["case_id"],),
    )
    connection.commit()
    before = connection.execute(
        "SELECT id,revision FROM conflict_cases WHERE id=?",
        (first["case_id"],),
    ).fetchone()
    new_member = _claim(repository, "claim-101", "8101")

    attached = _ensure(repository, [new_member])

    after = connection.execute(
        "SELECT id,revision FROM conflict_cases WHERE id=?",
        (first["case_id"],),
    ).fetchone()
    state = connection.execute(
        "SELECT dirty_at,dirty_reason FROM conflict_review_state WHERE case_id=?",
        (first["case_id"],),
    ).fetchone()
    assert attached["outcome"] == "attached"
    assert tuple(after) == (before["id"], before["revision"] + 1)
    assert state["dirty_at"] is not None
    assert state["dirty_reason"] == "candidate_set_changed"
    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1


def test_same_canonical_value_folds_into_one_candidate_and_support_count(tmp_path: Path) -> None:
    connection = Database(tmp_path / "candidate-fold.db").open()
    repository = ClaimRepository(connection)
    same_a = _claim(repository, "same-a", "8080")
    same_b = _claim(repository, "same-b", "8080")
    different = _claim(repository, "different", "8081")

    result = _ensure(repository, [same_a, same_b, different])

    rows = connection.execute(
        "SELECT canonical_value_json,support_count FROM conflict_case_candidates "
        "WHERE case_id=? ORDER BY canonical_value_json",
        (result["case_id"],),
    ).fetchall()
    assert [tuple(row) for row in rows] == [('"8080"', 2), ('"8081"', 1)]
    assert result["candidate_count"] == 2
    assert result["revision"] == 2


def test_new_case_for_terminal_group_uses_next_generation(tmp_path: Path) -> None:
    connection = Database(tmp_path / "generation.db").open()
    repository = ClaimRepository(connection)
    first = _ensure(
        repository,
        [_claim(repository, "generation-1-a", "8080"), _claim(repository, "generation-1-b", "8081")],
    )
    connection.execute(
        "UPDATE conflict_cases SET status='resolved',decision='test_terminal',resolved_at=? WHERE id=?",
        (NOW, first["case_id"]),
    )
    connection.commit()

    second = _ensure(
        repository,
        [_claim(repository, "generation-2-a", "8082"), _claim(repository, "generation-2-b", "8083")],
    )

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert second["case_id"] != first["case_id"]
    assert connection.execute(
        "SELECT count(*) FROM conflict_cases WHERE namespace_key='default' AND group_key='gateway-port'"
    ).fetchone()[0] == 2


def test_candidate_overflow_marks_single_case_manual_without_dropping_members(tmp_path: Path) -> None:
    settings = Settings.for_test()
    connection = Database(tmp_path / "overflow.db", settings=settings).open()
    repository = ClaimRepository(connection, settings=settings)
    members = [_claim(repository, f"claim-{index}", str(index)) for index in range(9)]

    result = _ensure(repository, members)

    case = connection.execute(
        "SELECT status,overflow FROM conflict_cases WHERE id=?",
        (result["case_id"],),
    ).fetchone()
    assert tuple(case) == ("manual_required", 1)
    assert result["candidate_count"] == 9
    assert connection.execute("SELECT count(*) FROM conflict_candidate_members").fetchone()[0] == 9


def test_candidate_overflow_never_enters_automatic_resolution(tmp_path: Path) -> None:
    connection = Database(tmp_path / "overflow-manual.db").open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index}", str(index)) for index in range(9)]
    connection.execute("UPDATE claims SET source_authority='high' WHERE id='claim-0'")
    connection.execute("UPDATE claims SET source_authority='low' WHERE id='claim-1'")
    connection.commit()
    members = [repository.get_claim(f"claim-{index}") for index in range(9)]
    result = _ensure(repository, [member for member in members if member is not None])

    resolved = auto_resolve_conflicts(connection, NOW)

    assert resolved["resolved"] == 0
    assert resolved["manual_stable"] == 1
    assert tuple(
        connection.execute(
            "SELECT status,overflow FROM conflict_cases WHERE id=?",
            (result["case_id"],),
        ).fetchone()
    ) == ("manual_required", 1)


def test_group_auto_resolution_never_selects_from_only_two_representatives(tmp_path: Path) -> None:
    connection = Database(tmp_path / "group-auto-set.db").open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index}", str(index)) for index in range(3)]
    connection.execute("UPDATE claims SET source_authority='high' WHERE id='claim-0'")
    connection.execute("UPDATE claims SET source_authority='low' WHERE id='claim-1'")
    connection.commit()
    members = [repository.get_claim(f"claim-{index}") for index in range(3)]
    result = _ensure(repository, [member for member in members if member is not None])

    resolved = auto_resolve_conflicts(connection, NOW)

    assert resolved["resolved"] == 0
    assert resolved["manual_stable"] == 1
    assert connection.execute(
        "SELECT status FROM conflict_cases WHERE id=?",
        (result["case_id"],),
    ).fetchone()[0] == "manual_required"
    assert connection.execute(
        "SELECT count(*) FROM claims WHERE status='disputed'"
    ).fetchone()[0] == 3


def test_terminal_representative_does_not_close_group_with_two_current_candidates(tmp_path: Path) -> None:
    connection = Database(tmp_path / "group-terminal-representative.db").open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index}", str(index)) for index in range(3)]
    result = _ensure(repository, members)
    connection.execute(
        "UPDATE claims SET status='retracted',valid_to=?,recorded_to=? WHERE id='claim-0'",
        (NOW, NOW),
    )
    connection.commit()

    resolved = auto_resolve_conflicts(connection, NOW)

    assert resolved["resolved"] == 0
    assert resolved["manual_stable"] == 1
    assert connection.execute(
        "SELECT status FROM conflict_cases WHERE id=?",
        (result["case_id"],),
    ).fetchone()[0] == "manual_required"
    assert connection.execute(
        "SELECT count(*) FROM claims WHERE status='disputed'"
    ).fetchone()[0] == 2


def test_group_closes_when_only_one_current_candidate_remains(tmp_path: Path) -> None:
    connection = Database(tmp_path / "group-single-current.db").open()
    repository = ClaimRepository(connection)
    members = [_claim(repository, f"claim-{index}", str(index)) for index in range(3)]
    result = _ensure(repository, members)
    connection.execute(
        "UPDATE claims SET status='retracted',valid_to=?,recorded_to=? WHERE id IN ('claim-0','claim-1')",
        (NOW, NOW),
    )
    connection.commit()

    resolved = auto_resolve_conflicts(connection, NOW)

    assert resolved["resolved"] == 1
    assert tuple(
        connection.execute(
            "SELECT status,decision FROM conflict_cases WHERE id=?",
            (result["case_id"],),
        ).fetchone()
    ) == ("resolved", "single_current_candidate")
    assert connection.execute("SELECT status FROM claims WHERE id='claim-2'").fetchone()[0] == "active"


@pytest.mark.parametrize("slot", ("config.path", "config.network"))
def test_nonexclusive_group_is_rejected_before_any_case_or_status_write(tmp_path: Path, slot: str) -> None:
    connection = Database(tmp_path / f"nonexclusive-{slot}.db").open()
    repository = ClaimRepository(connection)
    left = _claim(repository, "left", "left", slot=slot, conflict_key=f"{slot}-group")
    right = _claim(repository, "right", "right", slot=slot, conflict_key=f"{slot}-group")
    before = [tuple(row) for row in connection.execute("SELECT id,status FROM claims ORDER BY id")]

    with pytest.raises(ConflictError, match="mutually-exclusive"):
        _ensure(repository, [left, right])

    assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
    assert [tuple(row) for row in connection.execute("SELECT id,status FROM claims ORDER BY id")] == before


def test_partial_unique_race_reselects_open_case_but_other_integrity_errors_escape(tmp_path: Path) -> None:
    connection = Database(tmp_path / "integrity.db").open()
    repository = ClaimRepository(connection)
    left = _claim(repository, "left", "8080")
    right = _claim(repository, "right", "8081")
    created = _ensure(repository, [left, right])
    third = _claim(repository, "third", "8082")

    attached = _ensure(repository, [third])

    assert attached["case_id"] == created["case_id"]
    connection.execute(
        "CREATE TRIGGER reject_candidate_insert BEFORE INSERT ON conflict_case_candidates "
        "BEGIN SELECT RAISE(ABORT,'candidate rejected'); END"
    )
    fourth = _claim(repository, "fourth", "8083")
    with pytest.raises(sqlite3.IntegrityError, match="candidate rejected"):
        _ensure(repository, [fourth])


def test_partial_unique_race_reselects_the_winning_open_case(tmp_path: Path) -> None:
    database = Database(tmp_path / "partial-unique-race.db", busy_timeout_seconds=5)
    setup = database.open()
    setup_repository = ClaimRepository(setup)
    members = [_claim(setup_repository, "left", "8080"), _claim(setup_repository, "right", "8081")]
    setup.close()
    barrier = threading.Barrier(2)

    class BarrierConnection:
        def __init__(self, connection: Any) -> None:
            self.connection = connection
            self.waited = False

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            cursor = self.connection.execute(sql, parameters)
            if (
                not self.waited
                and sql.startswith("SELECT * FROM conflict_cases WHERE namespace_key=? AND group_key=?")
            ):
                self.waited = True
                barrier.wait(timeout=5)
            return cursor

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

    def create_or_attach() -> dict[str, object]:
        connection = database.open()
        try:
            repository = ClaimRepository(BarrierConnection(connection))
            return _ensure(repository, members)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_or_attach(), range(2)))

    connection = database.open()
    try:
        assert len({result["case_id"] for result in results}) == 1
        assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM conflict_case_candidates").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM conflict_candidate_members").fetchone()[0] == 2
    finally:
        connection.close()
