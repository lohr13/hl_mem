"""到期 Claim 的 TTL 关闭与历史可见性维护。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from hl_mem.domain.claims.retention import normalize_utc_iso
from hl_mem.lifecycle import assert_transition


def expire_claims(
    connection: sqlite3.Connection,
    now: str | None = None,
    feedback_lifecycle_mode: str | None = None,
    slot_short_ttl_seconds: int | None = None,
) -> dict[str, int]:
    """过期 expires_at 已到达且仍处于 active 的 claim。"""
    reference = normalize_utc_iso(now or datetime.now(timezone.utc).isoformat(), "now")
    candidate_cutoff = (datetime.fromisoformat(reference).astimezone(timezone.utc) + timedelta(days=180)).isoformat(
        timespec="seconds"
    )
    mode = feedback_lifecycle_mode or os.getenv("HL_MEM_FEEDBACK_LIFECYCLE_MODE", "observe").lower()
    short_ttl_seconds = (
        slot_short_ttl_seconds
        if slot_short_ttl_seconds is not None
        else int(os.getenv("HL_MEM_SLOT_SHORT_TTL_SECONDS", "86400"))
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT c.id,c.status,c.expires_at,c.valid_to,c.canonical_slot,c.observed_at,c.recorded_from,"
            "COALESCE(u.retention_bonus_days,0) AS bonus_days FROM claims c "
            "LEFT JOIN memory_usefulness u ON u.memory_type='claim' AND u.memory_id=c.id "
            "WHERE c.status=? AND c.expires_at IS NOT NULL AND c.expires_at<=?",
            ("active", candidate_cutoff),
        ).fetchall()
        expired_claims: list[tuple[str, str, str, str | None]] = []
        for row in rows:
            base = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            effective = base
            if mode == "on" and row["bonus_days"] > 0:
                effective = base + timedelta(days=row["bonus_days"])
                if row["valid_to"]:
                    effective = min(effective, datetime.fromisoformat(row["valid_to"].replace("Z", "+00:00")))
                if row["canonical_slot"] == "state.service_health":
                    anchor = datetime.fromisoformat((row["observed_at"] or row["recorded_from"]).replace("Z", "+00:00"))
                    effective = min(effective, anchor + timedelta(seconds=short_ttl_seconds))
            if effective <= datetime.fromisoformat(reference):
                assert_transition(row["status"], "expired")
                expired_claims.append(
                    (row["id"], effective.isoformat(timespec="seconds"), row["expires_at"], row["valid_to"])
                )
        cursor_count = 0
        for claim_id, effective_expire, expires_at, valid_to in expired_claims:
            cursor = connection.execute(
                "UPDATE claims SET status='expired',valid_to=CASE WHEN valid_to IS NULL OR ?<valid_to "
                "THEN ? ELSE valid_to END WHERE id=? AND status=? AND expires_at=? AND valid_to IS ?",
                (effective_expire, effective_expire, claim_id, "active", expires_at, valid_to),
            )
            cursor_count += cursor.rowcount
        connection.commit()
        return {"expired": cursor_count}
    except Exception:
        connection.rollback()
        raise
