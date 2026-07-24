from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from hl_mem.lifecycle import assert_transition


def expire_claims(connection: sqlite3.Connection, now: str | None = None) -> dict[str, int]:
    """过期 expires_at 已到达且仍处于 active 的 claim。"""
    reference = now or datetime.now(timezone.utc).isoformat()
    rows = connection.execute(
        "SELECT status FROM claims WHERE status='active' "
        "AND expires_at IS NOT NULL AND expires_at<=?",
        (reference,),
    ).fetchall()
    for row in rows:
        assert_transition(row[0], "expired")
    cursor = connection.execute(
        "UPDATE claims SET status='expired',valid_to=CASE "
        "WHEN valid_to IS NULL OR expires_at<valid_to THEN expires_at ELSE valid_to END "
        "WHERE status='active' "
        "AND expires_at IS NOT NULL AND expires_at<=?",
        (reference,),
    )
    connection.commit()
    return {"expired": cursor.rowcount}
