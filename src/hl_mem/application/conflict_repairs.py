"""Classify and safely repair dangling conflict-case references."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.conflict_invariants import find_dangling_conflict_references

TERMINAL_CONFLICT_CASE_STATUSES = ("resolved", "rejected")
DEFAULT_REPAIR_LIMIT = 100
AUDIT_CASE_ID_LIMIT = 20


def _category(status: str, left_exists: bool, right_exists: bool) -> str:
    if status not in TERMINAL_CONFLICT_CASE_STATUSES:
        return "open_dangling"
    if not left_exists and not right_exists:
        return "terminal_both_missing"
    return "terminal_one_side"


def inspect_dangling_conflicts(connection: Any) -> list[dict[str, Any]]:
    """Return all dangling conflict cases with endpoint and action metadata."""
    dangling_ids = {item["id"] for item in find_dangling_conflict_references(connection)}
    if not dangling_ids:
        return []
    rows = connection.execute(
        "SELECT cases.id,cases.status,cases.left_claim_id,cases.right_claim_id,"
        "left_claim.id AS left_exists,right_claim.id AS right_exists "
        "FROM conflict_cases AS cases "
        "LEFT JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
        "LEFT JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
        "WHERE left_claim.id IS NULL OR right_claim.id IS NULL ORDER BY cases.id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        case_id = str(row["id"])
        if case_id not in dangling_ids:
            continue
        status = str(row["status"])
        left_exists = row["left_exists"] is not None
        right_exists = row["right_exists"] is not None
        category = _category(status, left_exists, right_exists)
        result.append(
            {
                "id": case_id,
                "status": status,
                "left_claim_id": str(row["left_claim_id"]),
                "right_claim_id": str(row["right_claim_id"]),
                "left_exists": left_exists,
                "right_exists": right_exists,
                "category": category,
                "suggested_action": "delete" if category == "terminal_both_missing" else "manual_review",
            }
        )
    return result


def count_dangling_conflicts(connection: Any) -> dict[str, int]:
    """Count dangling cases by the health-check categories."""
    row = connection.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN cases.status IN ('resolved','rejected') "
        "AND left_claim.id IS NULL AND right_claim.id IS NULL THEN 1 ELSE 0 END),0) "
        "AS terminal_both_missing,"
        "COALESCE(SUM(CASE WHEN cases.status IN ('resolved','rejected') "
        "AND ((left_claim.id IS NULL) <> (right_claim.id IS NULL)) THEN 1 ELSE 0 END),0) "
        "AS terminal_one_side,"
        "COALESCE(SUM(CASE WHEN cases.status NOT IN ('resolved','rejected') "
        "AND (left_claim.id IS NULL OR right_claim.id IS NULL) THEN 1 ELSE 0 END),0) "
        "AS open_dangling "
        "FROM conflict_cases AS cases "
        "LEFT JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
        "LEFT JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id"
    ).fetchone()
    return {
        "terminal_both_missing": int(row["terminal_both_missing"]),
        "terminal_one_side": int(row["terminal_one_side"]),
        "open_dangling": int(row["open_dangling"]),
    }


def repair_dangling_conflicts(
    connection: Any,
    *,
    limit: int = DEFAULT_REPAIR_LIMIT,
    source: str,
) -> dict[str, Any]:
    """Delete one bounded batch of terminal cases whose endpoints are both absent."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT cases.id FROM conflict_cases AS cases "
            "LEFT JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
            "LEFT JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
            "WHERE cases.status IN ('resolved','rejected') "
            "AND left_claim.id IS NULL AND right_claim.id IS NULL "
            "ORDER BY cases.id LIMIT ?",
            (limit,),
        ).fetchall()
        case_ids = [str(row["id"]) for row in rows]
        deleted_count = 0
        if case_ids:
            placeholders = ",".join("?" for _ in case_ids)
            cursor = connection.execute(
                f"DELETE FROM conflict_cases WHERE id IN ({placeholders})",
                case_ids,
            )
            deleted_count = int(cursor.rowcount)
            detail = {
                "item": "repair_dangling_conflicts",
                "source": source,
                "deleted_count": deleted_count,
                "case_ids": case_ids[:AUDIT_CASE_ID_LIMIT],
                "case_ids_truncated": len(case_ids) > AUDIT_CASE_ID_LIMIT,
            }
            connection.execute(
                "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,detail_json) " "VALUES (?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    "worker",
                    "maintenance",
                    "success",
                    uuid.uuid4().hex,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                ),
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {
        "deleted_count": deleted_count,
        "deleted_case_ids": case_ids,
    }
