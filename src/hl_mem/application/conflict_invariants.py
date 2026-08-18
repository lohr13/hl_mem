"""冲突 mutation 的事务内共享 postcondition。"""

from __future__ import annotations

import sqlite3
from typing import Any

from hl_mem.domain.claims.attributes import MUTUALLY_EXCLUSIVE_SLOTS
from hl_mem.errors import ActiveClaimInvariantError, ConflictResolutionError

_OPEN_CONFLICT_CASE_STATUSES = ("pending", "auto_resolved", "manual_required")


def find_orphan_disputed_claims(connection: Any) -> list[str]:
    """返回没有任何 open conflict case 支撑的 disputed claim。"""
    placeholders = ",".join("?" for _ in _OPEN_CONFLICT_CASE_STATUSES)
    rows = connection.execute(
        "SELECT claims.id FROM claims WHERE claims.status='disputed' AND NOT EXISTS ("
        "SELECT 1 FROM conflict_cases AS cases "
        "LEFT JOIN conflict_candidate_members AS members "
        "ON members.case_id=cases.id AND members.claim_id=claims.id "
        "WHERE (cases.left_claim_id=claims.id OR cases.right_claim_id=claims.id OR members.claim_id IS NOT NULL) "
        f"AND cases.status IN ({placeholders}) AND cases.resolved_at IS NULL"
        ") ORDER BY claims.id",
        _OPEN_CONFLICT_CASE_STATUSES,
    ).fetchall()
    return [str(row["id"]) for row in rows]


def assert_no_orphan_disputed_claims(connection: Any) -> None:
    """拒绝裁决提交前断言没有不可见且失去复核入口的 disputed claim。"""
    orphan_ids = find_orphan_disputed_claims(connection)
    if orphan_ids:
        raise ConflictResolutionError(f"orphan disputed claim: {orphan_ids[0]}")


def find_dangling_conflict_references(connection: Any) -> list[dict[str, str]]:
    """返回引用缺失 Claim 的 conflict case。"""
    rows = connection.execute(
        "SELECT cases.id,cases.left_claim_id,cases.right_claim_id "
        "FROM conflict_cases AS cases "
        "LEFT JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
        "LEFT JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
        "WHERE left_claim.id IS NULL OR right_claim.id IS NULL ORDER BY cases.id"
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "left_claim_id": str(row["left_claim_id"]),
            "right_claim_id": str(row["right_claim_id"]),
        }
        for row in rows
    ]


def _active_group_violations(
    connection: sqlite3.Connection,
    *,
    namespace: str | None,
    conflict_key: str | None,
) -> list[dict[str, Any]]:
    slots = tuple(sorted(MUTUALLY_EXCLUSIVE_SLOTS))
    placeholders = ",".join("?" for _ in slots)
    conditions = [
        "status='active'",
        "conflict_key IS NOT NULL",
        f"canonical_slot IN ({placeholders})",
    ]
    parameters: list[Any] = list(slots)
    if namespace is not None:
        conditions.append("namespace_key=?")
        parameters.append(namespace)
    if conflict_key is not None:
        conditions.append("conflict_key=?")
        parameters.append(conflict_key)
    rows = connection.execute(
        "SELECT namespace_key,conflict_key,count(*) AS active_count FROM claims "
        f"WHERE {' AND '.join(conditions)} GROUP BY namespace_key,conflict_key HAVING count(*)>1 "
        "ORDER BY namespace_key,conflict_key",
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def assert_conflict_postconditions(
    connection: sqlite3.Connection,
    *,
    namespace: str | None = None,
    conflict_key: str | None = None,
) -> None:
    """提交前断言互斥组唯一 active 且 conflict case 引用完整。"""
    violations = _active_group_violations(
        connection,
        namespace=namespace,
        conflict_key=conflict_key,
    )
    if violations:
        first = violations[0]
        raise ActiveClaimInvariantError(
            "conflict postcondition found "
            f"{first['active_count']} active claims in group "
            f"{first['namespace_key']}:{first['conflict_key']}"
        )
    dangling = find_dangling_conflict_references(connection)
    if dangling:
        raise ConflictResolutionError(
            f"dangling conflict reference: {dangling[0]['id']} "
            f"({dangling[0]['left_claim_id']}, {dangling[0]['right_claim_id']})"
        )


def assert_conflict_case_postconditions(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    namespace: str | None,
    conflict_key: str | None,
    touched_claim_ids: list[str] | tuple[str, ...],
) -> None:
    """只验证单案事务触达的引用、互斥组和 disputed 支撑。"""

    dangling = connection.execute(
        "SELECT cases.id,cases.left_claim_id,cases.right_claim_id "
        "FROM conflict_cases AS cases "
        "LEFT JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
        "LEFT JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
        "WHERE cases.id=? AND (left_claim.id IS NULL OR right_claim.id IS NULL)",
        (case_id,),
    ).fetchone()
    if dangling is not None:
        raise ConflictResolutionError(
            f"dangling conflict reference: {dangling['id']} "
            f"({dangling['left_claim_id']}, {dangling['right_claim_id']})"
        )

    violations = _active_group_violations(
        connection,
        namespace=namespace,
        conflict_key=conflict_key,
    )
    if violations:
        first = violations[0]
        raise ActiveClaimInvariantError(
            "conflict postcondition found "
            f"{first['active_count']} active claims in group "
            f"{first['namespace_key']}:{first['conflict_key']}"
        )

    claim_ids = list(dict.fromkeys(str(claim_id) for claim_id in touched_claim_ids))
    if not claim_ids:
        return
    placeholders = ",".join("?" for _ in claim_ids)
    open_placeholders = ",".join("?" for _ in _OPEN_CONFLICT_CASE_STATUSES)
    orphan = connection.execute(
        "SELECT claims.id FROM claims "
        f"WHERE claims.id IN ({placeholders}) AND claims.status='disputed' AND NOT EXISTS ("
        "SELECT 1 FROM conflict_cases AS cases "
        "LEFT JOIN conflict_candidate_members AS members "
        "ON members.case_id=cases.id AND members.claim_id=claims.id "
        "WHERE (cases.left_claim_id=claims.id OR cases.right_claim_id=claims.id OR members.claim_id IS NOT NULL) "
        f"AND cases.status IN ({open_placeholders}) AND cases.resolved_at IS NULL"
        ") ORDER BY claims.id LIMIT 1",
        (*claim_ids, *_OPEN_CONFLICT_CASE_STATUSES),
    ).fetchone()
    if orphan is not None:
        raise ConflictResolutionError(f"orphan disputed claim: {orphan['id']}")


def assert_global_conflict_postconditions(connection: sqlite3.Connection) -> None:
    """在有界批次完成后执行一次全局只读巡检。"""

    assert_conflict_postconditions(connection)
