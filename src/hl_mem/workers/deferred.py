"""维护循环驱动的有界待处理任务。"""

from __future__ import annotations

import base64
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.http_utils import find_http_status_error
from hl_mem.lifecycle import assert_transition
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.deferred_tasks import DeferredTaskRepository
from hl_mem.storage.experience import ExperienceRepository
from hl_mem.storage.jobs import JobRepository

EXTRACTION_RETRY_DELAYS = (timedelta(hours=1), timedelta(hours=4), timedelta(hours=12))
DEFERRED_EXTRACTION_MAX_ATTEMPTS = len(EXTRACTION_RETRY_DELAYS)
ACTIVE_JOB_POSTPONE = timedelta(minutes=15)
RECALL_SIDE_EFFECT_RETRY_DELAY = timedelta(seconds=1)
RECALL_SIDE_EFFECT_TASK_TYPES = (
    "record_recall_access",
    "record_recall_exposures",
    "apply_retrieval_feedback",
    "mark_recall_feedback_injected",
    "resurrect_recalled_claim",
)
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
    disabled_task_types: frozenset[str] = frozenset(),
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
        if task["task_type"] in disabled_task_types:
            if repository.abandon_task(task["id"], "disabled_by_configuration", current_text):
                counts["abandoned"] += 1
            continue
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
        try:
            outcome = handler(connection, task, current)
        except Exception as error:
            if task["task_type"] not in RECALL_SIDE_EFFECT_TASK_TYPES:
                raise
            if connection.in_transaction:
                connection.rollback()
            repository.record_failure(
                task["id"],
                run_after=(current + RECALL_SIDE_EFFECT_RETRY_DELAY).isoformat(),
                error=str(error),
                updated_at=current_text,
            )
            counts["postponed"] += 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def process_recall_side_effect_tasks(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    limit: int = 100,
    disabled_task_types: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """高频消费召回副作用，不执行 legacy extraction 注册扫描。"""
    current_text = now or datetime.now(timezone.utc).isoformat()
    current = datetime.fromisoformat(current_text.replace("Z", "+00:00"))
    repository = DeferredTaskRepository(connection)
    counts = {"completed": 0, "retried": 0, "abandoned": 0}
    for task in repository.list_exhausted(limit=limit, task_types=RECALL_SIDE_EFFECT_TASK_TYPES):
        if repository.abandon_task(task["id"], "deferred retry budget exhausted", current_text):
            counts["abandoned"] += 1
    for task in repository.list_due(current_text, limit=limit, task_types=RECALL_SIDE_EFFECT_TASK_TYPES):
        if task["task_type"] in disabled_task_types:
            if repository.abandon_task(task["id"], "disabled_by_configuration", current_text):
                counts["abandoned"] += 1
            continue
        handler = DEFERRED_TASK_HANDLERS[task["task_type"]]
        try:
            outcome = handler(connection, task, current)
        except Exception as error:
            if connection.in_transaction:
                connection.rollback()
            if repository.record_failure(
                task["id"],
                run_after=(current + RECALL_SIDE_EFFECT_RETRY_DELAY).isoformat(),
                error=str(error),
                updated_at=current_text,
            ):
                counts["retried"] += 1
            continue
        if outcome == "completed":
            counts["completed"] += 1
    return counts


def cleanup_recall_side_effect_tasks(
    connection: sqlite3.Connection,
    *,
    before: str,
    limit: int = 1000,
) -> int:
    """有界清理保留期外的召回副作用终态任务。"""
    return DeferredTaskRepository(connection).cleanup_terminal(
        before,
        task_types=RECALL_SIDE_EFFECT_TASK_TYPES,
        limit=limit,
    )


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


def _record_recall_access(connection: sqlite3.Connection, task: dict[str, Any], now: datetime) -> str:
    payload = task["payload"]
    claim_ids = payload.get("claim_ids")
    accessed_at = payload.get("accessed_at")
    if not isinstance(claim_ids, list) or any(not isinstance(claim_id, str) for claim_id in claim_ids):
        raise ValueError("record_recall_access claim_ids must be a string array")
    if not isinstance(accessed_at, str):
        raise ValueError("record_recall_access accessed_at must be a string")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = DeferredTaskRepository(connection).get(task["id"])
        if current is None or current["status"] != "pending":
            connection.rollback()
            return "postponed"
        ClaimRepository(connection).record_access(claim_ids, accessed_at, commit=False)
        if not DeferredTaskRepository(connection).complete_task(task["id"], now.isoformat(), commit=False):
            raise RuntimeError("record_recall_access task completion lost")
        connection.commit()
        return "completed"
    except Exception:
        connection.rollback()
        raise


def _record_recall_exposures(connection: sqlite3.Connection, task: dict[str, Any], now: datetime) -> str:
    raw_exposures = task["payload"].get("exposures")
    if not isinstance(raw_exposures, list):
        raise ValueError("record_recall_exposures exposures must be an array")
    feedback: list[tuple[Any, ...]] = []
    for exposure in raw_exposures:
        if not isinstance(exposure, list) or len(exposure) != 7:
            raise ValueError("record_recall_exposures item must contain seven values")
        feedback.append((*exposure[:6], 0, None, None, exposure[6]))
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = DeferredTaskRepository(connection).get(task["id"])
        if current is None or current["status"] != "pending":
            connection.rollback()
            return "postponed"
        ExperienceRepository(connection).record_feedback_batch(feedback)
        if not DeferredTaskRepository(connection).complete_task(task["id"], now.isoformat(), commit=False):
            raise RuntimeError("record_recall_exposures task completion lost")
        connection.commit()
        return "completed"
    except Exception:
        connection.rollback()
        raise


def _apply_retrieval_feedback(connection: sqlite3.Connection, task: dict[str, Any], now: datetime) -> str:
    payload = task["payload"]
    feedback_id = payload.get("feedback_id")
    helpful = payload.get("helpful")
    task_outcome = payload.get("task_outcome")
    created_at = payload.get("created_at")
    if not isinstance(feedback_id, str) or not feedback_id:
        raise ValueError("apply_retrieval_feedback feedback_id must be a non-empty string")
    if not isinstance(helpful, bool):
        raise ValueError("apply_retrieval_feedback helpful must be a boolean")
    if task_outcome is not None and (
        isinstance(task_outcome, bool)
        or not isinstance(task_outcome, (int, float))
        or not 0.0 <= float(task_outcome) <= 1.0
    ):
        raise ValueError("apply_retrieval_feedback task_outcome must be between 0 and 1")
    if not isinstance(created_at, str):
        raise ValueError("apply_retrieval_feedback created_at must be a string")
    connection.execute("BEGIN IMMEDIATE")
    try:
        repository = DeferredTaskRepository(connection)
        current = repository.get(task["id"])
        if current is None or current["status"] != "pending":
            connection.rollback()
            return "postponed"
        ExperienceRepository(
            connection,
            settings=getattr(connection, "hl_mem_settings", None),
        ).submit_retrieval_feedback(
            feedback_id,
            helpful,
            float(task_outcome) if task_outcome is not None else None,
            created_at,
            commit=False,
        )
        if not repository.complete_task(task["id"], now.isoformat(), commit=False):
            raise RuntimeError("apply_retrieval_feedback task completion lost")
        connection.commit()
        return "completed"
    except Exception:
        connection.rollback()
        raise


def _mark_recall_feedback_injected(connection: sqlite3.Connection, task: dict[str, Any], now: datetime) -> str:
    feedback_ids = task["payload"].get("feedback_ids")
    if (
        not isinstance(feedback_ids, list)
        or not feedback_ids
        or any(not isinstance(feedback_id, str) or not feedback_id for feedback_id in feedback_ids)
    ):
        raise ValueError("mark_recall_feedback_injected feedback_ids must be a non-empty string array")
    connection.execute("BEGIN IMMEDIATE")
    try:
        repository = DeferredTaskRepository(connection)
        current = repository.get(task["id"])
        if current is None or current["status"] != "pending":
            connection.rollback()
            return "postponed"
        ExperienceRepository(connection).mark_feedback_injected_batch(feedback_ids)
        if not repository.complete_task(task["id"], now.isoformat(), commit=False):
            raise RuntimeError("mark_recall_feedback_injected task completion lost")
        connection.commit()
        return "completed"
    except Exception:
        connection.rollback()
        raise


def _resurrection_source_is_complete(connection: sqlite3.Connection, claim_id: str) -> bool:
    rows = connection.execute(
        "SELECT e.evidence_type,source_event.id AS event_id,source_claim.id AS claim_id,"
        "source_claim.status AS claim_status FROM evidence_links e "
        "LEFT JOIN events source_event ON e.evidence_type='event' AND source_event.id=e.evidence_id "
        "LEFT JOIN claims source_claim ON e.evidence_type='claim' AND source_claim.id=e.evidence_id "
        "WHERE e.derived_type='claim' AND e.derived_id=?",
        (claim_id,),
    ).fetchall()
    if not rows:
        return False
    return all(
        (row["evidence_type"] == "event" and row["event_id"] is not None)
        or (
            row["evidence_type"] == "claim"
            and row["claim_id"] is not None
            and row["claim_status"] not in {"candidate", "retracted"}
        )
        for row in rows
    )


def _resurrection_has_active_rival(connection: sqlite3.Connection, claim: dict[str, Any]) -> bool:
    conflict_key = claim.get("conflict_key")
    if not conflict_key:
        return False
    return (
        connection.execute(
            "SELECT 1 FROM claims WHERE namespace_key=? AND conflict_key=? " "AND status='active' AND id<>? LIMIT 1",
            (claim.get("namespace_key"), conflict_key, claim.get("id")),
        ).fetchone()
        is not None
    )


def _resurrect_recalled_claim(connection: sqlite3.Connection, task: dict[str, Any], now: datetime) -> str:
    payload = task["payload"]
    required = ("claim_id", "embedding_base64", "embedding_model", "embedding_dim", "namespace", "as_of")
    if any(payload.get(key) is None for key in required):
        raise ValueError("resurrect_recalled_claim payload is incomplete")
    try:
        embedding = base64.b64decode(str(payload["embedding_base64"]), validate=True)
    except ValueError as error:
        raise ValueError("resurrect_recalled_claim embedding is invalid") from error
    claim_id = str(payload["claim_id"])
    namespace = str(payload["namespace"])
    as_of = str(payload["as_of"])
    known_as_of = payload.get("known_as_of")
    connection.execute("BEGIN IMMEDIATE")
    try:
        repository = DeferredTaskRepository(connection)
        current = repository.get(task["id"])
        if current is None or current["status"] != "pending":
            connection.rollback()
            return "postponed"
        claims = ClaimRepository(connection)
        claim = claims.get_claim(claim_id)
        eligible = bool(
            claim is not None
            and claim.get("status") == "archived"
            and claim.get("namespace_key") == namespace
            and claim_is_visible(
                {**claim, "status": "active"},
                as_of,
                str(known_as_of) if known_as_of is not None else None,
                RecallIntent.CURRENT_STATE,
            )
            and _resurrection_source_is_complete(connection, claim_id)
            and not _resurrection_has_active_rival(connection, claim)
        )
        if eligible:
            assert claim is not None
            assert_transition("archived", "active")
            cursor = connection.execute(
                "UPDATE claims SET status='active',embedding_dense=?,embedding_sparse=NULL,"
                "embedding_model=?,embedding_dim=? WHERE id=? AND status='archived'",
                (
                    embedding,
                    str(payload["embedding_model"]),
                    int(payload["embedding_dim"]),
                    claim_id,
                ),
            )
            if cursor.rowcount == 1:
                claims.sync_vector(claim_id)
        if not repository.complete_task(task["id"], now.isoformat(), commit=False):
            raise RuntimeError("resurrect_recalled_claim task completion lost")
        connection.commit()
        return "completed"
    except Exception:
        connection.rollback()
        raise


DEFERRED_TASK_HANDLERS: dict[str, DeferredTaskHandler] = {
    "retry_extract_event": _schedule_extract_retry,
    "record_recall_access": _record_recall_access,
    "record_recall_exposures": _record_recall_exposures,
    "apply_retrieval_feedback": _apply_retrieval_feedback,
    "mark_recall_feedback_injected": _mark_recall_feedback_injected,
    "resurrect_recalled_claim": _resurrect_recalled_claim,
}
