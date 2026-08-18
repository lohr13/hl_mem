"""M5 冲突归并 worker 测试。"""

import pytest

from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.consolidate import (
    ConflictConsolidator,
    ConsolidationDecision,
    auto_resolve_conflicts,
    enqueue_daily_consolidation,
)


class Judge:
    def __init__(self, kind="compatible", confidence=1.0, current_claim_id=None):
        self.decision = ConsolidationDecision(kind, confidence, "测试", current_claim_id)

    def judge(self, _left, _right):
        return self.decision


def _claim(connection, claim_id, vector, **values):
    row = {
        "id": claim_id,
        "namespace_key": "default",
        "subject_entity_id": "用户",
        "canonical_attribute": "choice.tool",
        "predicate": "使用",
        "value_json": f'"{claim_id}"',
        "status": "active",
        "scope": "permanent",
        "valid_from": f"2026-01-0{claim_id == 'b' and 2 or 1}T00:00:00Z",
        "recorded_from": "2026-01-01T00:00:00Z",
        "embedding_dense": pack_vector(vector),
        "embedding_model": "fake-v1",
    }
    row.update(values)
    assert ClaimRepository(connection).insert_claim(row)


def test_candidate_thresholds_and_pair_idempotency(tmp_path) -> None:
    connection = Database(tmp_path / "pairs.db").open()
    _claim(connection, "a", [1.0, 0.0])
    _claim(connection, "b", [0.8, 0.6])
    worker = ConflictConsolidator(connection, Judge())
    assert [(pair.left["id"], pair.right["id"]) for pair in worker.scan_candidates("default", None, 10)] == [("a", "b")]
    assert worker.run_batch(10)["reviewed"] == 1
    assert worker.run_batch(10)["reviewed"] == 0


def test_state_change_supersedes_and_low_confidence_does_not_mutate(tmp_path) -> None:
    connection = Database(tmp_path / "state.db").open()
    _claim(connection, "a", [1.0, 0.0])
    _claim(connection, "b", [0.8, 0.6])
    result = ConflictConsolidator(connection, Judge("state_change", 1.0, "b")).run_batch(10)
    assert result["state_change"] == 1
    assert ClaimRepository(connection).get_claim("a")["status"] == "superseded"

    other = Database(tmp_path / "low.db").open()
    _claim(other, "a", [1.0, 0.0])
    _claim(other, "b", [0.8, 0.6])
    assert (
        ConflictConsolidator(other, Judge("contradiction", 0.1), confidence_threshold=0.8).run_batch(10)[
            "manual_review"
        ]
        == 1
    )
    assert {row[0] for row in other.execute("SELECT status FROM claims")} == {"active"}


def test_daily_scheduler_is_idempotent_and_configurable(tmp_path) -> None:
    connection = Database(tmp_path / "schedule.db").open()
    assert enqueue_daily_consolidation(connection, "2026-07-22T03:29:00+00:00", "03:30") is False
    assert enqueue_daily_consolidation(connection, "2026-07-22T03:30:00+00:00", "03:30") is True
    assert enqueue_daily_consolidation(connection, "2026-07-22T12:00:00+00:00", "03:30") is False
    row = connection.execute("SELECT job_type,idempotency_key FROM jobs").fetchone()
    assert tuple(row) == ("consolidate_conflicts", "consolidate:2026-07-22")


def _conflict_case(
    connection,
    case_id: str,
    left_claim_id: str,
    right_claim_id: str,
    *,
    status: str = "auto_resolved",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    assert ClaimRepository(connection).insert_conflict_case(
        {
            "id": case_id,
            "pair_key": f"{left_claim_id}:{right_claim_id}",
            "left_claim_id": left_claim_id,
            "right_claim_id": right_claim_id,
            "status": status,
            "created_at": created_at,
        }
    )


def _auto_conflict_case(connection, *, status: str = "auto_resolved") -> None:
    _claim(connection, "a", [1.0, 0.0], status="disputed", source_authority="high")
    _claim(connection, "b", [0.8, 0.6], status="disputed", source_authority="low")
    _conflict_case(connection, "case", "a", "b", status=status)


def _assert_auto_stats(result, *, scanned: int, resolved: int, manual: int, deferred: int, failed: int = 0) -> None:
    assert {
        "scanned": result["scanned"],
        "auto_resolved": result["auto_resolved"],
        "manual_required": result["manual_required"],
        "deferred": result["deferred"],
        "failed": result["failed"],
    } == {
        "scanned": scanned,
        "auto_resolved": resolved,
        "manual_required": manual,
        "deferred": deferred,
        "failed": failed,
    }


def test_auto_resolve_conflict_commits_winner_loser_and_case_together(tmp_path) -> None:
    connection = Database(tmp_path / "auto-resolve.db").open()
    _auto_conflict_case(connection)
    resolved_at = "2026-01-03T04:05:06+00:00"

    _assert_auto_stats(
        auto_resolve_conflicts(connection, resolved_at),
        scanned=1,
        resolved=1,
        manual=0,
        deferred=0,
    )

    winner = connection.execute("SELECT status FROM claims WHERE id='a'").fetchone()
    loser = connection.execute(
        "SELECT status,superseded_by_id,valid_to,recorded_to FROM claims WHERE id='b'"
    ).fetchone()
    case = connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    assert winner["status"] == "active"
    assert tuple(loser) == ("superseded", "a", resolved_at, resolved_at)
    assert tuple(case) == ("resolved", "keep_left", resolved_at)


def test_auto_resolve_conflict_rolls_back_entire_case_on_failure(tmp_path) -> None:
    connection = Database(tmp_path / "auto-resolve-rollback.db").open()
    _auto_conflict_case(connection)
    connection.execute(
        "CREATE TRIGGER reject_conflict_resolution BEFORE UPDATE OF status ON conflict_cases "
        "WHEN NEW.status='resolved' BEGIN SELECT RAISE(ABORT,'reject resolution'); END"
    )
    connection.commit()

    result = auto_resolve_conflicts(connection, "2026-01-03T04:05:06+00:00")
    _assert_auto_stats(result, scanned=1, resolved=0, manual=0, deferred=1, failed=1)

    claims = connection.execute(
        "SELECT id,status,superseded_by_id,valid_to,recorded_to FROM claims ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in claims] == [
        ("a", "disputed", None, None, None),
        ("b", "disputed", None, None, None),
    ]
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("auto_resolved", None, None)
    assert connection.in_transaction is False


def test_auto_resolve_conflict_failure_does_not_block_later_cases(tmp_path) -> None:
    connection = Database(tmp_path / "auto-resolve-continue.db").open()
    _auto_conflict_case(connection)
    _claim(connection, "c", [1.0, 0.0], status="disputed", source_authority="high")
    _claim(connection, "d", [0.8, 0.6], status="disputed", source_authority="low")
    _conflict_case(
        connection,
        "case-2",
        "c",
        "d",
        created_at="2026-01-02T00:00:00+00:00",
    )
    connection.execute(
        "CREATE TRIGGER reject_first_resolution BEFORE UPDATE OF status ON conflict_cases "
        "WHEN OLD.id='case' AND NEW.status='resolved' BEGIN SELECT RAISE(ABORT,'reject first'); END"
    )
    connection.commit()

    result = auto_resolve_conflicts(connection, "2026-01-03T04:05:06+00:00")
    _assert_auto_stats(result, scanned=2, resolved=1, manual=0, deferred=1, failed=1)

    first = connection.execute(
        "SELECT id,status,superseded_by_id FROM claims WHERE id IN ('a','b') ORDER BY id"
    ).fetchall()
    second = connection.execute(
        "SELECT id,status,superseded_by_id FROM claims WHERE id IN ('c','d') ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in first] == [("a", "disputed", None), ("b", "disputed", None)]
    assert [tuple(row) for row in second] == [("c", "active", None), ("d", "superseded", "c")]
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-2'").fetchone()[0] == "resolved"


def test_auto_resolve_scans_manual_required_cases(tmp_path) -> None:
    connection = Database(tmp_path / "manual-required.db").open()
    _auto_conflict_case(connection, status="manual_required")
    resolved_at = "2026-01-03T04:05:06+00:00"

    _assert_auto_stats(
        auto_resolve_conflicts(connection, resolved_at),
        scanned=1,
        resolved=1,
        manual=0,
        deferred=0,
    )
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("resolved", "keep_left", resolved_at)


def test_auto_resolve_marks_converged_chains_obsolete(tmp_path) -> None:
    connection = Database(tmp_path / "same-chain-tip.db").open()
    repository = ClaimRepository(connection)
    _claim(connection, "left", [1.0, 0.0])
    _claim(connection, "right", [0.8, 0.6])
    _claim(connection, "tip", [0.9, 0.1])
    changed_at = "2026-01-02T00:00:00+00:00"
    assert repository.supersede_with_inline("left", "tip", "tip", changed_at, changed_at).applied
    assert repository.supersede_with_inline("right", "tip", "tip", changed_at, changed_at).applied
    _conflict_case(connection, "case", "left", "right", status="pending")
    resolved_at = "2026-01-03T04:05:06+00:00"

    _assert_auto_stats(
        auto_resolve_conflicts(connection, resolved_at),
        scanned=1,
        resolved=1,
        manual=0,
        deferred=0,
    )
    assert repository.get_claim("tip")["status"] == "active"
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("resolved", "obsolete", resolved_at)


def test_auto_resolve_keeps_living_endpoint_when_other_is_terminal(tmp_path) -> None:
    connection = Database(tmp_path / "single-terminal.db").open()
    repository = ClaimRepository(connection)
    _claim(connection, "left", [1.0, 0.0])
    _claim(connection, "right", [0.8, 0.6], status="disputed")
    assert repository.update_status("left", "expired")
    _conflict_case(connection, "case", "left", "right", status="pending")
    resolved_at = "2026-01-03T04:05:06+00:00"

    _assert_auto_stats(
        auto_resolve_conflicts(connection, resolved_at),
        scanned=1,
        resolved=1,
        manual=0,
        deferred=0,
    )
    assert repository.get_claim("left")["status"] == "expired"
    assert repository.get_claim("right")["status"] == "active"
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("resolved", "keep_right", resolved_at)


def test_auto_resolve_does_not_activate_survivor_with_another_open_case(tmp_path) -> None:
    connection = Database(tmp_path / "contested-survivor.db").open()
    repository = ClaimRepository(connection)
    _claim(connection, "terminal", [1.0, 0.0])
    _claim(connection, "survivor", [0.8, 0.6], status="disputed", source_authority="medium")
    _claim(connection, "rival", [0.9, 0.1], status="disputed", source_authority="medium")
    assert repository.update_status("terminal", "expired")
    _conflict_case(connection, "case-1", "terminal", "survivor", status="pending")
    _conflict_case(
        connection,
        "case-2",
        "survivor",
        "rival",
        status="manual_required",
        created_at="2026-01-02T00:00:00+00:00",
    )

    _assert_auto_stats(
        auto_resolve_conflicts(connection, "2026-01-03T04:05:06+00:00"),
        scanned=2,
        resolved=1,
        manual=1,
        deferred=1,
    )
    assert repository.get_claim("survivor")["status"] == "disputed"


def test_auto_resolve_treats_chain_alias_as_same_contested_survivor(tmp_path) -> None:
    connection = Database(tmp_path / "aliased-contested-survivor.db").open()
    repository = ClaimRepository(connection)
    _claim(connection, "terminal", [1.0, 0.0])
    _claim(connection, "survivor", [0.9, 0.1], status="disputed", source_authority="high")
    _claim(connection, "alias", [0.8, 0.2])
    _claim(connection, "rival", [0.7, 0.3], status="disputed", source_authority="low")
    assert repository.update_status("terminal", "expired")
    assert repository.supersede_with_inline(
        "alias",
        "survivor",
        "survivor",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    ).applied
    _conflict_case(connection, "case-1", "terminal", "survivor", status="pending")
    _conflict_case(
        connection,
        "case-2",
        "alias",
        "rival",
        status="manual_required",
        created_at="2026-01-02T00:00:00+00:00",
    )

    _assert_auto_stats(
        auto_resolve_conflicts(connection, "2026-01-03T04:05:06+00:00"),
        scanned=2,
        resolved=2,
        manual=0,
        deferred=0,
    )
    assert repository.get_claim("survivor")["status"] == "active"
    assert repository.get_claim("rival")["status"] == "superseded"
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-2'").fetchone()[0] == "resolved"


def test_auto_resolve_marks_two_terminal_endpoints_obsolete(tmp_path) -> None:
    connection = Database(tmp_path / "double-terminal.db").open()
    repository = ClaimRepository(connection)
    _claim(connection, "left", [1.0, 0.0])
    _claim(connection, "right", [0.8, 0.6])
    assert repository.update_status("left", "expired")
    assert repository.update_status("right", "retracted")
    _conflict_case(connection, "case", "left", "right", status="manual_required")
    resolved_at = "2026-01-03T04:05:06+00:00"

    _assert_auto_stats(
        auto_resolve_conflicts(connection, resolved_at),
        scanned=1,
        resolved=1,
        manual=0,
        deferred=0,
    )
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("resolved", "obsolete", resolved_at)


def test_auto_resolve_keeps_equal_authority_case_manual_required(tmp_path) -> None:
    connection = Database(tmp_path / "authority-tie.db").open()
    _claim(connection, "left", [1.0, 0.0], status="disputed", source_authority="medium")
    _claim(connection, "right", [0.8, 0.6], status="disputed", source_authority="medium")
    _conflict_case(connection, "case", "left", "right", status="manual_required")

    _assert_auto_stats(
        auto_resolve_conflicts(connection, "2026-01-03T04:05:06+00:00"),
        scanned=1,
        resolved=0,
        manual=1,
        deferred=1,
    )
    assert tuple(
        connection.execute("SELECT status,decision,resolved_at FROM conflict_cases WHERE id='case'").fetchone()
    ) == ("manual_required", None, None)


def test_auto_resolve_begin_failure_does_not_block_later_cases(tmp_path) -> None:
    connection = Database(tmp_path / "begin-failure.db").open()
    _auto_conflict_case(connection)
    _claim(connection, "c", [1.0, 0.0], status="disputed", source_authority="high")
    _claim(connection, "d", [0.8, 0.6], status="disputed", source_authority="low")
    _conflict_case(
        connection,
        "case-2",
        "c",
        "d",
        created_at="2026-01-02T00:00:00+00:00",
    )

    class FailFirstBegin:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
            self.failed = False

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE" and not self.failed:
                self.failed = True
                raise RuntimeError("reject first begin")
            return self.wrapped.execute(sql, parameters)

        @property
        def in_transaction(self):
            return self.wrapped.in_transaction

        def commit(self) -> None:
            self.wrapped.commit()

        def rollback(self) -> None:
            self.wrapped.rollback()

    result = auto_resolve_conflicts(FailFirstBegin(connection), "2026-01-03T04:05:06+00:00")
    _assert_auto_stats(result, scanned=2, resolved=1, manual=0, deferred=1, failed=1)

    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case'").fetchone()[0] == "auto_resolved"
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-2'").fetchone()[0] == "resolved"
