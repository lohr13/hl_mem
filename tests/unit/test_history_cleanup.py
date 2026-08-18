from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.application.ingest import _insert_pending_dedup_pair
from hl_mem.monitoring.metrics import AdmissionMetrics
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.usefulness import UsefulnessRepository
from hl_mem.workers.history_cleanup import HistoryCleanupPolicy, cleanup_operational_history

NOW = "2026-08-18T08:00:00+00:00"
OLD = "2025-01-01T00:00:00+00:00"
FRESH = "2026-08-17T00:00:00+00:00"


def _insert_job(connection, job_id: str, status: str, updated_at: str) -> None:
    connection.execute(
        "INSERT INTO jobs("
        "id,job_type,status,attempts,max_attempts,created_at,updated_at"
        ") VALUES (?,'test',?,0,3,?,?)",
        (job_id, status, updated_at, updated_at),
    )


def _insert_span(connection, span_id: str, started_at: str) -> None:
    connection.execute(
        "INSERT INTO llm_call_spans("
        "span_id,trace_id,operation,provider,model,status,started_at,completed_at"
        ") VALUES (?,?,'test','fake','fake','success',?,?)",
        (span_id, f"trace-{span_id}", started_at, started_at),
    )


def _insert_feedback(
    connection,
    feedback_id: str,
    *,
    injected: int,
    created_at: str,
    helpful: int | None = None,
    task_outcome: float | None = None,
) -> None:
    connection.execute(
        "INSERT INTO retrieval_feedback("
        "id,query_id,memory_type,memory_id,injected,helpful,task_outcome,created_at"
        ") VALUES (?,?,'claim','claim',?,?,?,?)",
        (feedback_id, f"query-{feedback_id}", injected, helpful, task_outcome, created_at),
    )


def _seed_claim_and_dedup(connection) -> None:
    connection.executemany(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,?,?,'active')",
        (("claim", '"claim"', OLD), ("other", '"other"', OLD), ("third", '"third"', OLD)),
    )
    connection.executemany(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,decision,reviewed_at,created_at"
        ") VALUES (?,?,?,?,0.95,?,?,?)",
        (
            ("dedup-old", "pair-old", "claim", "other", "distinct", OLD, OLD),
            ("dedup-fresh", "pair-fresh", "claim", "third", "equivalent", FRESH, FRESH),
            ("dedup-pending", "pair-pending", "other", "third", None, None, OLD),
        ),
    )


def test_cleanup_applies_each_retention_policy_without_deleting_live_or_labeled_rows(tmp_path: Path) -> None:
    connection = Database(tmp_path / "semantics.db").open()
    _seed_claim_and_dedup(connection)
    for job_id, status, updated_at in (
        ("succeeded-old", "succeeded", OLD),
        ("succeeded-fresh", "succeeded", FRESH),
        ("dead-old", "dead", OLD),
        ("failed-old", "failed", OLD),
        ("pending-old", "pending", OLD),
        ("running-old", "running", OLD),
    ):
        _insert_job(connection, job_id, status, updated_at)
    _insert_span(connection, "span-old", OLD)
    _insert_span(connection, "span-fresh", FRESH)
    _insert_feedback(connection, "uninjected-old", injected=0, created_at=OLD)
    _insert_feedback(connection, "uninjected-fresh", injected=0, created_at=FRESH)
    _insert_feedback(connection, "injected-old", injected=1, created_at=OLD)
    _insert_feedback(connection, "injected-fresh", injected=1, created_at=FRESH)
    _insert_feedback(connection, "labeled-old", injected=1, created_at=OLD, helpful=1, task_outcome=0.8)
    connection.commit()
    repository = UsefulnessRepository(connection)
    assert repository.rebuild_all() == 1
    before_usefulness = tuple(
        connection.execute(
            "SELECT helpful_count,unhelpful_count,success_sum,outcome_count FROM memory_usefulness "
            "WHERE memory_type='claim' AND memory_id='claim'"
        ).fetchone()
    )

    result = cleanup_operational_history(
        connection,
        NOW,
        HistoryCleanupPolicy(batch_size=20),
    )

    assert result == {
        "jobs_deleted": 3,
        "jobs_failed": 0,
        "spans_deleted": 1,
        "spans_failed": 0,
        "dedup_deleted": 1,
        "dedup_failed": 0,
        "feedback_deleted": 2,
        "feedback_failed": 0,
        "remaining_expired": 0,
        "failures": 0,
    }
    assert {row[0] for row in connection.execute("SELECT id FROM jobs")} == {
        "succeeded-fresh",
        "pending-old",
        "running-old",
    }
    assert {row[0] for row in connection.execute("SELECT span_id FROM llm_call_spans")} == {"span-fresh"}
    assert {row[0] for row in connection.execute("SELECT id FROM dedup_pairs")} == {
        "dedup-fresh",
        "dedup-pending",
    }
    assert {row[0] for row in connection.execute("SELECT id FROM retrieval_feedback")} == {
        "uninjected-fresh",
        "injected-fresh",
        "labeled-old",
    }
    assert repository.rebuild_all() == 1
    after_usefulness = tuple(
        connection.execute(
            "SELECT helpful_count,unhelpful_count,success_sum,outcome_count FROM memory_usefulness "
            "WHERE memory_type='claim' AND memory_id='claim'"
        ).fetchone()
    )
    assert after_usefulness == before_usefulness


def test_cleanup_is_bounded_and_reports_remaining_expired(tmp_path: Path) -> None:
    connection = Database(tmp_path / "bounded.db").open()
    for index in range(3):
        _insert_span(connection, f"span-{index}", OLD)
    connection.commit()
    policy = HistoryCleanupPolicy(batch_size=2)

    first = cleanup_operational_history(connection, NOW, policy)
    second = cleanup_operational_history(connection, NOW, policy)

    assert first["spans_deleted"] == 2
    assert first["remaining_expired"] == 1
    assert second["spans_deleted"] == 1
    assert second["remaining_expired"] == 0


def test_cleanup_rolls_back_one_table_failure_and_continues(tmp_path: Path) -> None:
    connection = Database(tmp_path / "failure-isolation.db").open()
    _insert_job(connection, "job-old", "succeeded", OLD)
    _insert_span(connection, "span-old", OLD)
    connection.execute(
        "CREATE TRIGGER reject_job_cleanup BEFORE DELETE ON jobs "
        "BEGIN SELECT RAISE(ABORT,'job cleanup rejected'); END"
    )
    connection.commit()

    result = cleanup_operational_history(connection, NOW, HistoryCleanupPolicy(batch_size=10))

    assert result["jobs_deleted"] == 0
    assert result["jobs_failed"] == 1
    assert result["spans_deleted"] == 1
    assert result["spans_failed"] == 0
    assert result["failures"] == 1
    assert connection.execute("SELECT count(*) FROM jobs WHERE id='job-old'").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM llm_call_spans").fetchone()[0] == 0


@pytest.mark.parametrize(
    "changes",
    (
        {"batch_size": 0},
        {"job_succeeded_days": 0},
        {"feedback_unlabeled_days": 0},
    ),
)
def test_cleanup_policy_requires_positive_values(changes: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        HistoryCleanupPolicy(**changes)


def test_pending_dedup_cap_skips_new_pair_and_increments_health_counter(tmp_path: Path) -> None:
    settings = replace(Settings.for_test(), dedup_max_pending_pairs=1)
    connection = Database(tmp_path / "dedup-cap.db", settings=settings).open()
    connection.executemany(
        "INSERT INTO claims(id,value_json,recorded_from,status) VALUES (?,?,?,'active')",
        (("existing", '"existing"', OLD), ("new-1", '"new-1"', OLD), ("new-2", '"new-2"', OLD)),
    )
    connection.commit()
    metrics = AdmissionMetrics()

    assert _insert_pending_dedup_pair(
        connection,
        "existing",
        {"id": "new-1", "namespace_key": "default", "predicate": "配置"},
        0.9,
        NOW,
        metrics=metrics,
    )
    assert not _insert_pending_dedup_pair(
        connection,
        "existing",
        {"id": "new-2", "namespace_key": "default", "predicate": "配置"},
        0.9,
        NOW,
        metrics=metrics,
    )

    assert connection.execute("SELECT count(*) FROM dedup_pairs WHERE decision IS NULL").fetchone()[0] == 1
    assert metrics.snapshot() == {"dedup_pending_pairs_skipped": 1}


def test_cleanup_releases_writer_between_bounded_table_transactions(tmp_path: Path) -> None:
    path = tmp_path / "writer-fairness.db"
    database = Database(path, busy_timeout_seconds=1)
    background = database.open()
    for index in range(20):
        _insert_span(background, f"span-{index}", OLD)
    background.commit()
    foreground = database.open()
    started = threading.Event()

    class SlowBegin:
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            self.wrapped = wrapped

        def execute(self, sql: str, parameters=()):
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
        target=cleanup_operational_history,
        args=(SlowBegin(background), NOW, HistoryCleanupPolicy(batch_size=5)),
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
    assert background.execute("SELECT count(*) FROM llm_call_spans").fetchone()[0] == 15
