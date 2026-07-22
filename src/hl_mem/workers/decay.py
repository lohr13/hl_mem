from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from hl_mem.lifecycle import assert_transition


def _load_policy() -> dict[str, tuple[int, int]]:
    """从环境变量加载记忆衰减与归档边界。"""
    temporal_decay = int(os.getenv("HL_MEM_DECAY_TEMPORAL_DAYS", "90"))
    temporal_archive = int(os.getenv("HL_MEM_DECAY_TEMPORAL_ARCHIVE", "180"))
    permanent_decay = int(os.getenv("HL_MEM_DECAY_PERMANENT_DAYS", "180"))
    permanent_archive = int(os.getenv("HL_MEM_DECAY_PERMANENT_ARCHIVE", "365"))
    return {
        "temporal": (temporal_decay, temporal_archive),
        "permanent": (permanent_decay, permanent_archive),
    }

# Access-frequency decay bonus: every ACCESS_BONUS_EVERY hits adds
# ACCESS_BONUS_DAYS to both decay_after and archive_after, capped at
# ACCESS_BONUS_CAP.  A frequently-recalled memory decays slower.
_ACCESS_BONUS_EVERY = int(os.getenv("HL_MEM_ACCESS_BONUS_EVERY", "10"))
_ACCESS_BONUS_DAYS = int(os.getenv("HL_MEM_ACCESS_BONUS_DAYS", "30"))
_ACCESS_BONUS_CAP = int(os.getenv("HL_MEM_ACCESS_BONUS_CAP", "365"))


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def decay_claims(
    connection: sqlite3.Connection,
    now: str | None = None,
    rollout_grace_days: int = 7,
    min_confidence: float = 0.05,
) -> dict[str, int]:
    """Linearly decay inactive claims and archive them at scope-specific boundaries."""
    reference = _parse(now) if now else datetime.now(timezone.utc)
    day_start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    minimum = min(1.0, max(0.0, float(min_confidence)))
    policy = _load_policy()
    decayed = archived = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        migration = connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE version='005_memory_management'"
        ).fetchone()
        migration_at = _parse(migration[0]) if migration else None
        grace_until = (migration_at + timedelta(days=rollout_grace_days)
                       if migration_at else None)
        rows = connection.execute(
            "SELECT id,scope,confidence,access_count,recorded_from,last_accessed_at,last_decayed_at,status "
            "FROM claims WHERE status IN ('active','disputed')"
        ).fetchall()
        for row in rows:
            claim = dict(row)
            anchor = _parse(claim["last_accessed_at"] or claim["recorded_from"])
            if (claim["last_accessed_at"] is None and migration_at and grace_until
                    and reference < grace_until and anchor <= migration_at):
                continue
            decay_after, archive_after = policy.get(claim["scope"], policy["permanent"])
            access_count = max(0, int(claim.get("access_count") or 0))
            bonus = min(
                access_count // _ACCESS_BONUS_EVERY * _ACCESS_BONUS_DAYS,
                _ACCESS_BONUS_CAP,
            )
            decay_after += bonus
            archive_after += bonus
            inactive_days = (reference - anchor).total_seconds() / 86400.0
            if inactive_days > archive_after:
                assert_transition(claim["status"], "archived")
                cursor = connection.execute(
                    "UPDATE claims SET status='archived',embedding_dense=NULL,embedding_sparse=NULL "
                    "WHERE id=? AND status IN ('active','disputed')", (claim["id"],))
                archived += cursor.rowcount
                continue
            if inactive_days <= decay_after:
                continue
            previous = _parse(claim["last_decayed_at"]) if claim["last_decayed_at"] else None
            if previous is not None and previous >= day_start:
                continue
            decay_start = anchor + timedelta(days=decay_after)
            elapsed_from = max(decay_start, previous) if previous else decay_start
            elapsed_days = int((day_start - elapsed_from).total_seconds() // 86400)
            if elapsed_days <= 0:
                continue
            daily_delta = (1.0 - minimum) / (archive_after - decay_after)
            confidence = max(minimum, float(claim["confidence"] or 0.0)
                             - daily_delta * elapsed_days)
            cursor = connection.execute(
                "UPDATE claims SET confidence=?,last_decayed_at=? "
                "WHERE id=? AND status IN ('active','disputed')",
                (confidence, reference.isoformat(), claim["id"]),
            )
            decayed += cursor.rowcount
        connection.commit()
        return {"decayed": decayed, "archived": archived}
    except Exception:
        connection.rollback()
        raise
