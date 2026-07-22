"""租户级写入配额与明确的数据保留操作。"""

from __future__ import annotations

import sqlite3


def enforce_event_quota(connection: sqlite3.Connection, tenant_id: str, maximum_events: int) -> None:
    """在租户达到事件配额时拒绝下一次写入。"""
    if maximum_events < 1:
        raise ValueError("maximum_events must be positive")
    count = connection.execute("SELECT count(*) FROM events WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
    if count >= maximum_events:
        raise ValueError(f"event quota exceeded for tenant: {tenant_id}")


def purge_retained_events(connection: sqlite3.Connection, tenant_id: str, recorded_before: str) -> int:
    """删除指定租户在保留边界前且没有 Claim 证据依赖的事件。"""
    cursor = connection.execute(
        "DELETE FROM events WHERE tenant_id=? AND recorded_at<? AND NOT EXISTS ("
        "SELECT 1 FROM evidence_links WHERE evidence_type='event' AND evidence_id=events.id)",
        (tenant_id, recorded_before),
    )
    connection.commit()
    return cursor.rowcount
