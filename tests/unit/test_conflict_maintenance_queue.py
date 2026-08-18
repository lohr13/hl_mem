from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hl_mem.storage.database import Database
from hl_mem.workers.consolidate import auto_resolve_conflicts

NOW = "2026-08-18T00:00:00+00:00"


def _claim(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    status: str = "disputed",
    authority: str = "medium",
    superseded_by_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,predicate,value_json,recorded_from,status,source_authority,superseded_by_id"
        ") VALUES (?, 'default', 'uses', ?, ?, ?, ?, ?)",
        (claim_id, f'"{claim_id}"', NOW, status, authority, superseded_by_id),
    )


def _case(
    connection: sqlite3.Connection,
    case_id: str,
    left_id: str,
    right_id: str,
    *,
    status: str = "pending",
    created_at: str = NOW,
) -> None:
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,created_at"
        ") VALUES (?,?,?,?,?,?)",
        (case_id, f"pair-{case_id}", left_id, right_id, status, created_at),
    )
    connection.commit()


def _manual_case(connection: sqlite3.Connection, case_id: str) -> None:
    _claim(connection, f"{case_id}-left")
    _claim(connection, f"{case_id}-right")
    _case(connection, case_id, f"{case_id}-left", f"{case_id}-right", status="manual_required")


def test_stable_manual_backlog_has_zero_scan_and_zero_writes(tmp_path: Path) -> None:
    connection = Database(tmp_path / "stable-manual.db").open()
    for index in range(500):
        _manual_case(connection, f"case-{index:03d}")
    connection.execute(
        "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='stable_fixture',input_fingerprint='stable'"
    )
    connection.commit()
    baseline = connection.total_changes

    first = auto_resolve_conflicts(connection, NOW)
    second = auto_resolve_conflicts(connection, NOW)

    for result in (first, second):
        assert result["eligible"] == 0
        assert result["scanned"] == 0
        assert result["changed"] == 0
        assert result["manual_stable"] == 0
    assert connection.total_changes == baseline


def test_dirty_manual_is_cleaned_once_then_never_written_again(tmp_path: Path) -> None:
    connection = Database(tmp_path / "manual-clean.db").open()
    _manual_case(connection, "case")

    first = auto_resolve_conflicts(connection, NOW)
    assert first["scanned"] == 1
    assert first["changed"] == 0
    assert first["manual_stable"] == 1
    state = connection.execute(
        "SELECT dirty_at,last_reviewed_at,input_fingerprint,left_tip_id,right_tip_id "
        "FROM conflict_review_state WHERE case_id='case'"
    ).fetchone()
    assert tuple(state[:2]) == (None, NOW)
    assert state[2]
    assert tuple(state[3:]) == ("case-left", "case-right")
    baseline = connection.total_changes

    assert auto_resolve_conflicts(connection, NOW)["changed"] == 0
    assert auto_resolve_conflicts(connection, NOW)["changed"] == 0
    assert connection.total_changes == baseline


def test_claim_input_change_requeues_clean_manual_case(tmp_path: Path) -> None:
    connection = Database(tmp_path / "manual-requeue.db").open()
    _manual_case(connection, "case")
    auto_resolve_conflicts(connection, NOW)
    assert connection.execute("SELECT dirty_at FROM conflict_review_state WHERE case_id='case'").fetchone()[0] is None

    connection.execute("UPDATE claims SET source_authority='high' WHERE id='case-left'")
    connection.commit()
    result = auto_resolve_conflicts(connection, "2026-08-18T00:01:00+00:00")

    assert result["scanned"] == 1
    assert result["resolved"] == 1
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case'").fetchone()[0] == "resolved"


def test_followed_tip_change_requeues_case_that_references_old_endpoint(tmp_path: Path) -> None:
    connection = Database(tmp_path / "tip-requeue.db").open()
    _claim(connection, "tip")
    _claim(connection, "alias", status="superseded", superseded_by_id="tip")
    _claim(connection, "right")
    _case(connection, "case", "alias", "right", status="manual_required")
    first = auto_resolve_conflicts(connection, NOW)
    assert first["manual_stable"] == 1
    assert (
        connection.execute("SELECT left_tip_id,dirty_at FROM conflict_review_state WHERE case_id='case'").fetchone()[0]
        == "tip"
    )

    connection.execute("UPDATE claims SET source_authority='high' WHERE id='tip'")
    connection.commit()
    assert (
        connection.execute("SELECT dirty_at FROM conflict_review_state WHERE case_id='case'").fetchone()[0] is not None
    )

    result = auto_resolve_conflicts(connection, "2026-08-18T00:01:00+00:00")
    assert result["resolved"] == 1
    assert connection.execute("SELECT status FROM claims WHERE id='tip'").fetchone()[0] == "active"
    assert connection.execute("SELECT status FROM claims WHERE id='right'").fetchone()[0] == "superseded"


@pytest.mark.parametrize("terminal_status", ("superseded", "expired"))
def test_terminal_endpoint_auto_closes_open_case(tmp_path: Path, terminal_status: str) -> None:
    connection = Database(tmp_path / f"terminal-{terminal_status}.db").open()
    if terminal_status == "superseded":
        _claim(connection, "winner", status="active", authority="high")
        _claim(connection, "left", status="superseded", superseded_by_id="winner")
        _claim(connection, "right", status="active")
    else:
        _claim(connection, "left", status="expired")
        _claim(connection, "right", status="disputed")
    _case(connection, "case", "left", "right")

    result = auto_resolve_conflicts(connection, NOW)

    assert result["resolved"] == 1
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case'").fetchone()[0] == "resolved"
    assert connection.execute("SELECT 1 FROM conflict_review_state WHERE case_id='case'").fetchone() is None


def test_count_budget_uses_persistent_cursor_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "cursor.db"
    database = Database(path)
    connection = database.open()
    for index in range(10):
        _manual_case(connection, f"case-{index:02d}")
    first = auto_resolve_conflicts(connection, NOW, max_cases=3)
    first_ids = {
        row[0]
        for row in connection.execute("SELECT case_id FROM conflict_review_state WHERE last_reviewed_at IS NOT NULL")
    }
    database.close()

    reopened_database = Database(path)
    reopened = reopened_database.open()
    second = auto_resolve_conflicts(reopened, "2026-08-18T00:01:00+00:00", max_cases=3)
    second_ids = {
        row[0]
        for row in reopened.execute("SELECT case_id FROM conflict_review_state WHERE last_reviewed_at IS NOT NULL")
    }

    assert first["scanned"] == 3
    assert first["budget_exhausted"] is True
    assert second["scanned"] == 3
    assert len(first_ids) == 3
    assert len(second_ids - first_ids) == 3
    assert (
        reopened.execute("SELECT cursor_id FROM maintenance_cursors WHERE task='auto_resolve_conflicts'").fetchone()[0]
        == second["cursor_id"]
    )


def test_time_budget_stops_before_starting_next_case(tmp_path: Path) -> None:
    connection = Database(tmp_path / "time-budget.db").open()
    for index in range(5):
        _manual_case(connection, f"case-{index}")

    class Clock:
        def __init__(self) -> None:
            self.value = -0.002

        def __call__(self) -> float:
            self.value += 0.002
            return self.value

    result = auto_resolve_conflicts(connection, NOW, max_elapsed_ms=1, monotonic=Clock())

    assert result["scanned"] == 1
    assert result["budget_exhausted"] is True


def test_poison_case_backs_off_without_blocking_next_case(tmp_path: Path) -> None:
    connection = Database(tmp_path / "poison.db").open()
    for case_id in ("case-a", "case-b"):
        _claim(connection, f"{case_id}-left", authority="high")
        _claim(connection, f"{case_id}-right", authority="low")
        _case(connection, case_id, f"{case_id}-left", f"{case_id}-right")
    connection.execute(
        "CREATE TRIGGER reject_poison BEFORE UPDATE OF status ON conflict_cases "
        "WHEN OLD.id='case-a' AND NEW.status='resolved' "
        "BEGIN SELECT RAISE(ABORT,'poison'); END"
    )
    connection.commit()

    result = auto_resolve_conflicts(connection, NOW, failure_backoff_seconds=300)

    state = connection.execute(
        "SELECT attempt_count,not_before,last_error FROM conflict_review_state WHERE case_id='case-a'"
    ).fetchone()
    assert result["failed"] == 1
    assert result["resolved"] == 1
    assert tuple(state[:2]) == (1, "2026-08-18T00:10:00+00:00")
    assert "poison" in state[2]
    assert connection.execute("SELECT status FROM conflict_cases WHERE id='case-b'").fetchone()[0] == "resolved"


def test_bounded_batch_releases_writer_before_full_backlog(tmp_path: Path) -> None:
    path = tmp_path / "writer-fairness.db"
    database = Database(path, busy_timeout_seconds=1)
    background_connection = database.open()
    for index in range(30):
        _manual_case(background_connection, f"case-{index:02d}")
    background_connection.commit()
    foreground = database.open()
    started = threading.Event()

    class SlowBegin:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self.wrapped = wrapped

        def execute(self, sql: str, parameters: tuple[object, ...] = ()):
            result = self.wrapped.execute(sql, parameters)
            if sql == "BEGIN IMMEDIATE":
                started.set()
                time.sleep(0.03)
            return result

        @property
        def in_transaction(self) -> bool:
            return self.wrapped.in_transaction

        def commit(self) -> None:
            self.wrapped.commit()

        def rollback(self) -> None:
            self.wrapped.rollback()

    thread = threading.Thread(
        target=auto_resolve_conflicts,
        args=(SlowBegin(background_connection), NOW),
        kwargs={"max_cases": 3},
        daemon=True,
    )
    thread.start()
    assert started.wait(timeout=1.0)
    began_at = time.monotonic()
    foreground.execute("BEGIN IMMEDIATE")
    waited = time.monotonic() - began_at
    foreground.rollback()
    thread.join(timeout=2.0)

    assert thread.is_alive() is False
    assert waited < 1.0
    assert (
        background_connection.execute(
            "SELECT count(*) FROM conflict_review_state WHERE dirty_at IS NOT NULL"
        ).fetchone()[0]
        == 27
    )
