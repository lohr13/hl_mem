"""冲突 mutation 的事务内共享 postcondition。"""

from __future__ import annotations

import sqlite3
from typing import Any

from hl_mem.domain.claims.attributes import MUTUALLY_EXCLUSIVE_SLOTS
from hl_mem.errors import ActiveClaimInvariantError, ConflictResolutionError


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
