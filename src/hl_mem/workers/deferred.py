"""维护循环驱动的有界待处理任务。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hl_mem.http_utils import find_http_status_error
from hl_mem.storage.deferred_tasks import DeferredTaskRepository
from hl_mem.storage.jobs import JobRepository

EXTRACTION_RETRY_DELAYS = (timedelta(hours=1), timedelta(hours=4), timedelta(hours=12))
DEFERRED_EXTRACTION_MAX_ATTEMPTS = len(EXTRACTION_RETRY_DELAYS)
ACTIVE_JOB_POSTPONE = timedelta(minutes=15)
DeferredTaskHandler = Callable[[sqlite3.Connection, dict[str, Any], datetime], str]


def handle_failed_extractions(
    connection: sqlite3.Connection,
    event_ids: list[str],
    error: BaseException,
    *,
    now: str,
) -> None:
    """只把耗尽普通 job 重试的 HTTP 429 Event 转入延后队列。"""
    if not event_ids:
        return
    repository = DeferredTaskRepository(connection)
    status_error = find_http_status_error(error)
    if status_error is None or status_error.response.status_code != 429:
        repository.abandon_resources("event", event_ids, str(error), now)
        return
    anchor = datetime.fromisoformat(now.replace("Z", "+00:00"))
    run_after = (anchor + EXTRACTION_RETRY_DELAYS[0]).isoformat()
    for event_id in event_ids:
        repository.defer(
            task_type="retry_extract_event",
            resource_type="event",
            resource_id=event_id,
            payload={"event_id": event_id},
            idempotency_key=f"retry_extract_event:{event_id}",
            run_after=run_after,
            max_attempts=DEFERRED_EXTRACTION_MAX_ATTEMPTS,
            error=str(error),
            updated_at=now,
        )
        task = repository.get_by_idempotency_key(f"retry_extract_event:{event_id}")
        if task is not None and task["status"] == "pending" and task["attempts"] >= task["max_attempts"]:
            repository.abandon_task(task["id"], "HTTP 429 deferred retry budget exhausted", now)


def complete_deferred_extractions(connection: sqlite3.Connection, event_ids: list[str], now: str) -> int:
    """任一成功提取路径都可收敛同 Event 的待办。"""
    return DeferredTaskRepository(connection).complete_resources("event", event_ids, now)


def process_deferred_tasks(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    limit: int = 20,
) -> dict[str, int]:
    """轮询通用待办并交给已注册的轻量 handler。"""
    current_text = now or datetime.now(timezone.utc).isoformat()
    current = datetime.fromisoformat(current_text.replace("Z", "+00:00"))
    repository = DeferredTaskRepository(connection)
    counts = {
        "registered": _register_legacy_rate_limited_extractions(connection, current, limit),
        "scheduled": 0,
        "abandoned": 0,
        "postponed": 0,
    }
    for task in repository.list_exhausted(limit=limit):
        if _has_active_extract_job(connection, task):
            continue
        if repository.abandon_task(task["id"], "deferred retry budget exhausted", current_text):
            counts["abandoned"] += 1
    for task in repository.list_due(current_text, limit=limit):
        handler = DEFERRED_TASK_HANDLERS.get(task["task_type"])
        if handler is None:
            repository.postpone(
                task["id"],
                (current + EXTRACTION_RETRY_DELAYS[0]).isoformat(),
                f"unsupported deferred task type: {task['task_type']}",
                current_text,
            )
            counts["postponed"] += 1
            continue
        outcome = handler(connection, task, current)
        counts[outcome] += 1
    return counts


def _register_legacy_rate_limited_extractions(
    connection: sqlite3.Connection,
    now: datetime,
    limit: int,
) -> int:
    """把升级前已 dead 的明确 HTTP 429 提取任务纳入同一有界队列。"""
    rows = connection.execute(
        "SELECT json_extract(j.payload_json,'$.event_id') AS event_id,MAX(j.updated_at) AS failed_at "
        "FROM jobs j JOIN events e ON e.id=json_extract(j.payload_json,'$.event_id') "
        "WHERE j.job_type='extract_event' AND j.status='dead' "
        "AND j.last_error LIKE 'Client error ''429 Too Many Requests''%' "
        "AND NOT EXISTS (SELECT 1 FROM evidence_links l "
        "WHERE l.evidence_type='event' AND l.evidence_id=e.id) "
        "AND NOT EXISTS (SELECT 1 FROM deferred_tasks d "
        "WHERE d.idempotency_key='retry_extract_event:' || e.id) "
        "GROUP BY e.id ORDER BY failed_at,e.id LIMIT ?",
        (limit,),
    ).fetchall()
    repository = DeferredTaskRepository(connection)
    registered = 0
    for row in rows:
        failed_at = datetime.fromisoformat(str(row["failed_at"]).replace("Z", "+00:00"))
        if failed_at.tzinfo is None:
            failed_at = failed_at.replace(tzinfo=timezone.utc)
        run_after = max(now, failed_at + EXTRACTION_RETRY_DELAYS[0]).isoformat()
        event_id = str(row["event_id"])
        if repository.defer(
            task_type="retry_extract_event",
            resource_type="event",
            resource_id=event_id,
            payload={"event_id": event_id},
            idempotency_key=f"retry_extract_event:{event_id}",
            run_after=run_after,
            max_attempts=DEFERRED_EXTRACTION_MAX_ATTEMPTS,
            error="legacy dead extraction: HTTP 429 Too Many Requests",
            updated_at=now.isoformat(),
        ):
            registered += 1
    return registered


def _schedule_extract_retry(connection: sqlite3.Connection, task: dict[str, Any], now: datetime) -> str:
    event_id = str(task["resource_id"])
    if connection.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone() is None:
        DeferredTaskRepository(connection).abandon_task(
            task["id"],
            "deferred extraction source event no longer exists",
            now.isoformat(),
        )
        return "abandoned"
    if _has_active_extract_job(connection, task):
        DeferredTaskRepository(connection).postpone(
            task["id"],
            (now + ACTIVE_JOB_POSTPONE).isoformat(),
            "matching extraction job is still active",
            now.isoformat(),
        )
        return "postponed"
    attempt_number = int(task["attempts"]) + 1
    next_delay_index = min(attempt_number, len(EXTRACTION_RETRY_DELAYS) - 1)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = DeferredTaskRepository(connection).get(task["id"])
        if current is None or current["status"] != "pending" or current["attempts"] >= current["max_attempts"]:
            connection.rollback()
            return "postponed"
        if _has_active_extract_job(connection, current):
            connection.rollback()
            DeferredTaskRepository(connection).postpone(
                task["id"],
                (now + ACTIVE_JOB_POSTPONE).isoformat(),
                "matching extraction job is still active",
                now.isoformat(),
            )
            return "postponed"
        inserted = JobRepository(connection).insert_job(
            {
                "id": uuid.uuid4().hex,
                "job_type": "extract_event",
                "payload": {"event_id": event_id, "deferred_task_id": task["id"]},
                "idempotency_key": f"deferred:{task['id']}:{attempt_number}",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "max_attempts": 3,
            },
            commit=False,
        )
        recorded = DeferredTaskRepository(connection).record_attempt(
            task["id"],
            run_after=(now + EXTRACTION_RETRY_DELAYS[next_delay_index]).isoformat(),
            updated_at=now.isoformat(),
            commit=False,
        )
        if not inserted or not recorded:
            connection.rollback()
            return "postponed"
        connection.commit()
        return "scheduled"
    except Exception:
        connection.rollback()
        raise


def _has_active_extract_job(connection: sqlite3.Connection, task: dict[str, Any]) -> bool:
    if task["task_type"] != "retry_extract_event":
        return False
    row = connection.execute(
        "SELECT 1 FROM jobs WHERE job_type='extract_event' AND status IN ('pending','running') "
        "AND json_extract(payload_json,'$.event_id')=? LIMIT 1",
        (task["resource_id"],),
    ).fetchone()
    return row is not None


DEFERRED_TASK_HANDLERS: dict[str, DeferredTaskHandler] = {
    "retry_extract_event": _schedule_extract_retry,
}
