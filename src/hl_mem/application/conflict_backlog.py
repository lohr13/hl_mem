"""存量非互斥冲突工单的 fail-closed 集合式修复。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.conflict_invariants import (
    assert_conflict_postconditions,
    find_dangling_conflict_references,
    find_orphan_disputed_claims,
)
from hl_mem.domain.claims.attributes import MUTUALLY_EXCLUSIVE_SLOTS
from hl_mem.errors import ConflictError

OPEN_CASE_STATUSES = ("pending", "auto_resolved", "manual_required")
INVALID_RATIONALES = ("ingest_dirty_active_group", "ingest_group_resolution")
REPAIR_MARKER = "v0.28.9_invalid_nonexclusive_group"


def _target_sql(select_clause: str) -> tuple[str, tuple[Any, ...]]:
    exclusive_slots = tuple(sorted(MUTUALLY_EXCLUSIVE_SLOTS))
    status_placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
    rationale_placeholders = ",".join("?" for _ in INVALID_RATIONALES)
    slot_placeholders = ",".join("?" for _ in exclusive_slots)
    sql = (
        f"SELECT {select_clause} FROM conflict_cases AS cases "
        "JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
        "JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
        f"WHERE cases.status IN ({status_placeholders}) AND cases.resolved_at IS NULL "
        f"AND cases.rationale IN ({rationale_placeholders}) "
        "AND left_claim.namespace_key=right_claim.namespace_key "
        "AND left_claim.conflict_key IS NOT NULL "
        "AND left_claim.conflict_key=right_claim.conflict_key "
        "AND left_claim.canonical_slot IS NOT NULL "
        "AND left_claim.canonical_slot=right_claim.canonical_slot "
        f"AND left_claim.canonical_slot NOT IN ({slot_placeholders})"
    )
    return sql, (*OPEN_CASE_STATUSES, *INVALID_RATIONALES, *exclusive_slots)


def _target_rows(connection: Any) -> list[Any]:
    sql, parameters = _target_sql("cases.id,cases.left_claim_id,cases.right_claim_id,left_claim.canonical_slot")
    return list(connection.execute(f"{sql} ORDER BY cases.id", parameters).fetchall())


def _outside_open_endpoints(
    connection: Any,
    *,
    target_case_ids: set[str],
    endpoint_ids: set[str],
) -> list[str]:
    if not target_case_ids or not endpoint_ids:
        return []
    status_placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
    rows = connection.execute(
        "SELECT cases.id,cases.left_claim_id,cases.right_claim_id,members.claim_id AS member_claim_id "
        "FROM conflict_cases AS cases "
        "LEFT JOIN conflict_candidate_members AS members ON members.case_id=cases.id "
        f"WHERE cases.status IN ({status_placeholders}) AND cases.resolved_at IS NULL",
        OPEN_CASE_STATUSES,
    ).fetchall()
    shared: set[str] = set()
    for row in rows:
        if str(row["id"]) in target_case_ids:
            continue
        attached = {
            str(claim_id)
            for claim_id in (row["left_claim_id"], row["right_claim_id"], row["member_claim_id"])
            if claim_id is not None
        }
        shared.update(attached & endpoint_ids)
    return sorted(shared)


def inspect_invalid_conflict_groups(connection: Any) -> dict[str, Any]:
    """只读识别由旧 ingest 路径制造的 open 非互斥工单。"""

    rows = _target_rows(connection)
    target_case_ids = {str(row["id"]) for row in rows}
    endpoint_ids = {str(claim_id) for row in rows for claim_id in (row["left_claim_id"], row["right_claim_id"])}
    cases_by_slot: dict[str, int] = {}
    for row in rows:
        slot = str(row["canonical_slot"])
        cases_by_slot[slot] = cases_by_slot.get(slot, 0) + 1
    endpoint_status_counts: dict[str, int] = {}
    if endpoint_ids:
        placeholders = ",".join("?" for _ in endpoint_ids)
        status_rows = connection.execute(
            f"SELECT status,count(*) AS item_count FROM claims WHERE id IN ({placeholders}) "
            "GROUP BY status ORDER BY status",
            sorted(endpoint_ids),
        ).fetchall()
        endpoint_status_counts = {str(row["status"]): int(row["item_count"]) for row in status_rows}
    outside_endpoint_ids = _outside_open_endpoints(
        connection,
        target_case_ids=target_case_ids,
        endpoint_ids=endpoint_ids,
    )
    open_count = int(
        connection.execute(
            "SELECT count(*) FROM conflict_cases "
            "WHERE status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL"
        ).fetchone()[0]
    )
    return {
        "candidate_case_count": len(rows),
        "cases_by_slot": dict(sorted(cases_by_slot.items())),
        "endpoint_count": len(endpoint_ids),
        "endpoint_status_counts": endpoint_status_counts,
        "disputed_to_activate": endpoint_status_counts.get("disputed", 0),
        "outside_open_endpoint_count": len(outside_endpoint_ids),
        "outside_open_endpoint_ids": outside_endpoint_ids[:20],
        "outside_open_endpoint_ids_truncated": len(outside_endpoint_ids) > 20,
        "remaining_open_count": open_count - len(rows),
    }


def repair_invalid_conflict_groups(
    connection: Any,
    *,
    expected_count: int,
    repaired_at: str | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """在一个 IMMEDIATE 事务中关闭目标工单并恢复无其他审核依赖的 disputed claims。"""

    if expected_count < 0:
        raise ValueError("expected_count must not be negative")
    if connection.in_transaction:
        raise ConflictError("invalid conflict group repair requires a clean connection")
    timestamp = repaired_at or datetime.now(timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        preview = inspect_invalid_conflict_groups(connection)
        actual_count = int(preview["candidate_case_count"])
        if actual_count != expected_count:
            raise ConflictError(
                f"invalid conflict group count mismatch: expected {expected_count}, found {actual_count}"
            )
        if preview["outside_open_endpoint_count"]:
            first = preview["outside_open_endpoint_ids"][0]
            raise ConflictError(f"target endpoint participates in an open case outside the repair target: {first}")

        connection.execute("DROP TABLE IF EXISTS temp.invalid_conflict_repair_targets")
        target_sql, target_parameters = _target_sql("cases.id AS case_id")
        connection.execute(
            f"CREATE TEMP TABLE invalid_conflict_repair_targets AS {target_sql}",
            target_parameters,
        )
        connection.execute("DROP TABLE IF EXISTS temp.invalid_conflict_repair_endpoints")
        connection.execute(
            "CREATE TEMP TABLE invalid_conflict_repair_endpoints AS "
            "SELECT cases.left_claim_id AS claim_id FROM conflict_cases AS cases "
            "JOIN invalid_conflict_repair_targets AS targets ON targets.case_id=cases.id "
            "UNION SELECT cases.right_claim_id FROM conflict_cases AS cases "
            "JOIN invalid_conflict_repair_targets AS targets ON targets.case_id=cases.id"
        )
        selected_count = int(connection.execute("SELECT count(*) FROM invalid_conflict_repair_targets").fetchone()[0])
        if selected_count != expected_count:
            raise ConflictError(
                f"invalid conflict group target changed during repair: expected {expected_count}, found {selected_count}"
            )

        closed = connection.execute(
            "UPDATE conflict_cases SET status='rejected',decision='reject',resolved_at=?,"
            "rationale=rationale || ';' || ? "
            "WHERE id IN (SELECT case_id FROM invalid_conflict_repair_targets)",
            (timestamp, REPAIR_MARKER),
        )
        connection.execute(
            "DELETE FROM conflict_review_state "
            "WHERE case_id IN (SELECT case_id FROM invalid_conflict_repair_targets)"
        )
        status_placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
        activated = connection.execute(
            "UPDATE claims SET status='active' WHERE status='disputed' "
            "AND id IN (SELECT claim_id FROM invalid_conflict_repair_endpoints) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM conflict_cases AS other "
            "LEFT JOIN conflict_candidate_members AS members ON members.case_id=other.id "
            f"WHERE other.status IN ({status_placeholders}) AND other.resolved_at IS NULL "
            "AND (other.left_claim_id=claims.id OR other.right_claim_id=claims.id OR members.claim_id=claims.id)"
            ")",
            OPEN_CASE_STATUSES,
        )

        invalid_open_count = int(inspect_invalid_conflict_groups(connection)["candidate_case_count"])
        if invalid_open_count:
            raise ConflictError(f"invalid nonexclusive conflict cases remain open: {invalid_open_count}")
        orphan_ids = find_orphan_disputed_claims(connection)
        if orphan_ids:
            raise ConflictError(f"orphan disputed claim after repair: {orphan_ids[0]}")
        dangling = find_dangling_conflict_references(connection)
        if dangling:
            raise ConflictError(f"dangling conflict case after repair: {dangling[0]['id']}")
        assert_conflict_postconditions(connection)
        remaining_open_count = int(
            connection.execute(
                "SELECT count(*) FROM conflict_cases "
                "WHERE status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL"
            ).fetchone()[0]
        )
        detail = {
            "item": "repair_invalid_conflict_groups",
            "source": source,
            "applied_case_count": int(closed.rowcount),
            "activated_claim_count": int(activated.rowcount),
            "endpoint_count": int(preview["endpoint_count"]),
            "cases_by_slot": preview["cases_by_slot"],
            "remaining_open_count": remaining_open_count,
            "marker": REPAIR_MARKER,
        }
        connection.execute(
            "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,detail_json) " "VALUES (?,?,?,?,?,?)",
            (
                timestamp,
                "maintenance",
                "repair_invalid_conflict_groups",
                "success",
                uuid.uuid4().hex,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )
        connection.execute("DROP TABLE invalid_conflict_repair_endpoints")
        connection.execute("DROP TABLE invalid_conflict_repair_targets")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {
        **preview,
        "dry_run": False,
        "applied_case_count": int(closed.rowcount),
        "activated_claim_count": int(activated.rowcount),
        "remaining_open_count": remaining_open_count,
        "invalid_open_count": invalid_open_count,
        "orphan_disputed_count": 0,
        "dangling_count": 0,
    }
