"""到期 Claim 的 TTL 关闭与历史可见性维护。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from hl_mem.lifecycle import assert_transition
from hl_mem.domain.claims.retention import normalize_utc_iso


def expire_claims(
    connection: sqlite3.Connection,
    now: str | None = None,
    feedback_lifecycle_mode: str | None = None,
) -> dict[str, int]:
    """过期 expires_at 已到达且仍处于 active 的 claim。"""
    reference = normalize_utc_iso(now or datetime.now(timezone.utc).isoformat(), "now")
    mode = feedback_lifecycle_mode or os.getenv("HL_MEM_FEEDBACK_LIFECYCLE_MODE", "observe").lower()
    rows = connection.execute(
        "SELECT c.id,c.status,c.expires_at,c.valid_to,c.canonical_slot,c.observed_at,c.recorded_from,"
        "COALESCE(u.retention_bonus_days,0) AS bonus_days FROM claims c "
        "LEFT JOIN memory_usefulness u ON u.memory_type='claim' AND u.memory_id=c.id "
        "WHERE c.status='active' AND c.scope!='permanent' AND c.expires_at IS NOT NULL",
    ).fetchall()
    expired_ids: list[str] = []
    for row in rows:
        base = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        effective = base
        if mode == "on" and row["bonus_days"] > 0:
            effective = base + timedelta(days=row["bonus_days"])
            if row["valid_to"]:
                effective = min(effective, datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00")))
            if row["canonical_slot"] == "state.service_health":
                anchor = datetime.fromisoformat((row["observed_at"] or row["recorded_from"]).replace("Z", "+00:00"))
                effective = min(
                    effective, anchor + timedelta(seconds=int(os.getenv("HL_MEM_SLOT_SHORT_TTL_SECONDS", "86400")))
                )
        if effective <= datetime.fromisoformat(reference):
            assert_transition(row["status"], "expired")
            expired_ids.append(row["id"])
    cursor_count = 0
    for claim_id in expired_ids:
        cursor = connection.execute(
            "UPDATE claims SET status='expired',valid_to=CASE WHEN valid_to IS NULL OR expires_at<valid_to "
            "THEN expires_at ELSE valid_to END WHERE id=? AND status='active'",
            (claim_id,),
        )
        cursor_count += cursor.rowcount
    connection.commit()
    return {"expired": cursor_count}
