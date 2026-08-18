"""有界清理可重建的运维历史，不触碰核心记忆与证据。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class HistoryCleanupPolicy:
    batch_size: int = 2_000
    job_succeeded_days: int = 30
    job_dead_days: int = 90
    llm_span_days: int = 30
    dedup_pair_days: int = 90
    feedback_uninjected_days: int = 7
    feedback_unlabeled_days: int = 90

    def __post_init__(self) -> None:
        if any(int(getattr(self, item.name)) < 1 for item in fields(self)):
            raise ValueError("history cleanup policy values must be positive")


def _parse_now(now: str) -> datetime:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("history cleanup now must include a timezone")
    return parsed


def _delete_batch(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[Any, ...],
) -> tuple[int, int]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(sql, parameters)
        connection.commit()
        return int(cursor.rowcount), 0
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        return 0, 1


def _remaining_expired(
    connection: sqlite3.Connection,
    *,
    job_succeeded_cutoff: str,
    job_dead_cutoff: str,
    span_cutoff: str,
    dedup_cutoff: str,
    feedback_uninjected_cutoff: str,
    feedback_unlabeled_cutoff: str,
) -> int:
    jobs = int(
        connection.execute(
            "SELECT count(*) FROM jobs WHERE "
            "(status='succeeded' AND updated_at<?) "
            "OR (status IN ('dead','failed') AND updated_at<?)",
            (job_succeeded_cutoff, job_dead_cutoff),
        ).fetchone()[0]
    )
    spans = int(
        connection.execute(
            "SELECT count(*) FROM llm_call_spans WHERE started_at<?",
            (span_cutoff,),
        ).fetchone()[0]
    )
    dedup = int(
        connection.execute(
            "SELECT count(*) FROM dedup_pairs WHERE decision IS NOT NULL AND reviewed_at<?",
            (dedup_cutoff,),
        ).fetchone()[0]
    )
    feedback = int(
        connection.execute(
            "SELECT count(*) FROM retrieval_feedback "
            "WHERE helpful IS NULL AND task_outcome IS NULL AND ("
            "(injected=0 AND created_at<?) OR (injected=1 AND created_at<?))",
            (feedback_uninjected_cutoff, feedback_unlabeled_cutoff),
        ).fetchone()[0]
    )
    return jobs + spans + dedup + feedback


def cleanup_operational_history(
    connection: sqlite3.Connection,
    now: str,
    policy: HistoryCleanupPolicy,
) -> dict[str, int]:
    """每表一个短事务，每表最多删除一个 batch，失败仅回滚该表。"""

    if connection.in_transaction:
        raise ValueError("operational history cleanup requires a clean connection")
    current = _parse_now(now)
    job_succeeded_cutoff = (current - timedelta(days=policy.job_succeeded_days)).isoformat()
    job_dead_cutoff = (current - timedelta(days=policy.job_dead_days)).isoformat()
    span_cutoff = (current - timedelta(days=policy.llm_span_days)).isoformat()
    dedup_cutoff = (current - timedelta(days=policy.dedup_pair_days)).isoformat()
    feedback_uninjected_cutoff = (current - timedelta(days=policy.feedback_uninjected_days)).isoformat()
    feedback_unlabeled_cutoff = (current - timedelta(days=policy.feedback_unlabeled_days)).isoformat()

    jobs_deleted, jobs_failed = _delete_batch(
        connection,
        "DELETE FROM jobs WHERE id IN ("
        "SELECT id FROM jobs WHERE "
        "(status='succeeded' AND updated_at<?) "
        "OR (status IN ('dead','failed') AND updated_at<?) "
        "ORDER BY updated_at,id LIMIT ?)",
        (job_succeeded_cutoff, job_dead_cutoff, policy.batch_size),
    )
    spans_deleted, spans_failed = _delete_batch(
        connection,
        "DELETE FROM llm_call_spans WHERE id IN ("
        "SELECT id FROM llm_call_spans WHERE started_at<? "
        "ORDER BY started_at,id LIMIT ?)",
        (span_cutoff, policy.batch_size),
    )
    dedup_deleted, dedup_failed = _delete_batch(
        connection,
        "DELETE FROM dedup_pairs WHERE id IN ("
        "SELECT id FROM dedup_pairs WHERE decision IS NOT NULL AND reviewed_at<? "
        "ORDER BY reviewed_at,id LIMIT ?)",
        (dedup_cutoff, policy.batch_size),
    )
    feedback_deleted, feedback_failed = _delete_batch(
        connection,
        "DELETE FROM retrieval_feedback WHERE id IN ("
        "SELECT id FROM retrieval_feedback "
        "WHERE helpful IS NULL AND task_outcome IS NULL AND ("
        "(injected=0 AND created_at<?) OR (injected=1 AND created_at<?)) "
        "ORDER BY created_at,id LIMIT ?)",
        (feedback_uninjected_cutoff, feedback_unlabeled_cutoff, policy.batch_size),
    )
    remaining_expired = _remaining_expired(
        connection,
        job_succeeded_cutoff=job_succeeded_cutoff,
        job_dead_cutoff=job_dead_cutoff,
        span_cutoff=span_cutoff,
        dedup_cutoff=dedup_cutoff,
        feedback_uninjected_cutoff=feedback_uninjected_cutoff,
        feedback_unlabeled_cutoff=feedback_unlabeled_cutoff,
    )
    failures = jobs_failed + spans_failed + dedup_failed + feedback_failed
    return {
        "jobs_deleted": jobs_deleted,
        "jobs_failed": jobs_failed,
        "spans_deleted": spans_deleted,
        "spans_failed": spans_failed,
        "dedup_deleted": dedup_deleted,
        "dedup_failed": dedup_failed,
        "feedback_deleted": feedback_deleted,
        "feedback_failed": feedback_failed,
        "remaining_expired": remaining_expired,
        "failures": failures,
    }
