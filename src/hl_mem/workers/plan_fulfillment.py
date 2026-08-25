"""Immediate and cursor-based scheduling for plan reconciliation."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Callable

from hl_mem.domain.governance import snapshot_fingerprint
from hl_mem.domain.plan_fulfillment import PLAN_FULFILLMENT_POLICY_VERSION
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.storage.plan_fulfillments import PlanFulfillmentRepository

_CURSOR_TASK = "plan_fulfillment_scan"


def plan_maintenance_items(connection: sqlite3.Connection, now: str, mode: str) -> list[tuple[str, Callable[[], Any]]]:
    if mode == "off":
        return []
    return [
        (
            "enqueue_plan_reconciliation_scan",
            lambda: enqueue_plan_reconciliation_scan(connection, now, mode=mode),
        )
    ]


def enqueue_plan_result(
    connection: sqlite3.Connection,
    result_claim_id: str,
    now: str,
    *,
    commit: bool = False,
) -> bool:
    return JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "reconcile_plan_result",
            "payload": {
                "result_claim_id": result_claim_id,
                "policy_version": PLAN_FULFILLMENT_POLICY_VERSION,
            },
            "idempotency_key": (f"reconcile_plan_result:{result_claim_id}:{PLAN_FULFILLMENT_POLICY_VERSION}"),
            "status": "pending",
            "run_after": now,
            "max_attempts": 5,
            "created_at": now,
            "updated_at": now,
        },
        commit=commit,
    )


def _select_results(connection: sqlite3.Connection, limit: int) -> list[Any]:
    prefix = (
        "SELECT c.id,c.recorded_from FROM claims AS c WHERE c.status='active' "
        "AND json_extract(c.qualifiers_json,'$.assertion_phase') "
        "IN ('execution','cancellation','replacement') "
        "AND NOT EXISTS (SELECT 1 FROM plan_outcomes AS po WHERE po.result_claim_id=c.id "
        "AND po.policy_version=? AND po.status='applied') "
    )
    cursor = connection.execute(
        "SELECT cursor_time,cursor_id FROM maintenance_cursors WHERE task=?", (_CURSOR_TASK,)
    ).fetchone()
    rows: list[Any] = []
    if cursor and cursor["cursor_time"] is not None:
        rows = connection.execute(
            prefix + "AND (c.recorded_from,c.id)>(?,?) ORDER BY c.recorded_from,c.id LIMIT ?",
            (
                PLAN_FULFILLMENT_POLICY_VERSION,
                cursor["cursor_time"],
                cursor["cursor_id"],
                limit,
            ),
        ).fetchall()
    if len(rows) < limit:
        seen = {str(row["id"]) for row in rows}
        wrapped = connection.execute(
            prefix + "ORDER BY c.recorded_from,c.id LIMIT ?",
            (PLAN_FULFILLMENT_POLICY_VERSION, limit),
        ).fetchall()
        rows.extend(row for row in wrapped if str(row["id"]) not in seen)
    return rows[:limit]


def _input_fingerprint(connection: sqlite3.Connection, result_id: str) -> str:
    claims = ClaimRepository(connection)
    result = claims.get_claim(result_id)
    if result is None:
        raise KeyError(result_id)
    repository = PlanFulfillmentRepository(connection)
    plans = repository.find_open_plans(result)
    pairs = repository.equivalent_pairs(str(plan["id"]) for plan in plans)
    return snapshot_fingerprint(
        {
            "result": {
                "id": result_id,
                "target": result.get("canonical_target_entity_id"),
                "qualifiers": result.get("qualifiers"),
                "valid_from": result.get("valid_from"),
            },
            "plans": [(plan["id"], plan.get("fact_hash"), plan.get("valid_to")) for plan in plans],
            "equivalent_pairs": pairs,
        }
    )


def enqueue_plan_reconciliation_scan(
    connection: sqlite3.Connection,
    now: str,
    *,
    mode: str,
    limit: int = 50,
) -> dict[str, int]:
    """Rotate over unresolved results and enqueue each unchanged input at most once."""

    if mode == "off":
        return {"scanned": 0, "enqueued": 0}
    if limit < 1:
        raise ValueError("plan reconciliation scan limit must be positive")
    selected = _select_results(connection, limit)
    enqueued = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in selected:
            result_id = str(row["id"])
            fingerprint = _input_fingerprint(connection, result_id)
            enqueued += int(
                JobRepository(connection).insert_job(
                    {
                        "id": uuid.uuid4().hex,
                        "job_type": "reconcile_plan_result",
                        "payload": {
                            "result_claim_id": result_id,
                            "input_fingerprint": fingerprint,
                            "policy_version": PLAN_FULFILLMENT_POLICY_VERSION,
                        },
                        "idempotency_key": (
                            f"reconcile_plan_result:{result_id}:"
                            f"{PLAN_FULFILLMENT_POLICY_VERSION}:{fingerprint}:{mode}"
                        ),
                        "status": "pending",
                        "run_after": now,
                        "max_attempts": 5,
                        "created_at": now,
                        "updated_at": now,
                    },
                    commit=False,
                )
            )
        if selected:
            last = selected[-1]
            connection.execute(
                "INSERT INTO maintenance_cursors(task,cursor_time,cursor_id,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(task) DO UPDATE SET cursor_time=excluded.cursor_time,"
                "cursor_id=excluded.cursor_id,updated_at=excluded.updated_at",
                (_CURSOR_TASK, last["recorded_from"], last["id"], now),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"scanned": len(selected), "enqueued": enqueued}
