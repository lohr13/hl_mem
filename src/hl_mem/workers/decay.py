"""Claim 置信度衰减与低活跃记忆归档任务。"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

from hl_mem.domain.claims.retention import is_protected_attribute
from hl_mem.lifecycle import assert_transition
from hl_mem.storage.claims import ClaimRepository


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def activation_at(base: float, *, inactive_days: float, half_life_days: int) -> float:
    """Return bounded exponential activation for a non-negative inactivity age."""

    if half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    bounded_base = min(1.0, max(0.0, float(base)))
    return bounded_base * 2 ** (-max(0.0, float(inactive_days)) / half_life_days)


def _halflife_scope(claim: dict[str, object]) -> str:
    raw_attribute = claim.get("canonical_attribute")
    attribute = str(raw_attribute) if raw_attribute is not None else None
    return "identity" if is_protected_attribute(attribute) else str(claim.get("scope"))


def _decay_halflife(
    connection: sqlite3.Connection,
    reference: datetime,
    *,
    model: str,
    temporal_half_life_days: int,
    permanent_half_life_days: int,
    identity_half_life_days: int,
    archive_threshold: float,
    archive_grace_days: int,
) -> dict[str, int]:
    """Apply one of the two feature-gated exponential decay arms."""

    day_start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    half_lives = {
        "temporal": temporal_half_life_days,
        "permanent": permanent_half_life_days,
        "identity": identity_half_life_days,
    }
    repository = ClaimRepository(connection)
    decayed = archived = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT id,scope,status,confidence,activation_base,activation,recorded_from,last_accessed_at,"
            "last_decayed_at,decay_below_since,canonical_attribute FROM claims "
            "WHERE status IN ('active','disputed')"
        ).fetchall()
        for row in rows:
            claim = dict(row)
            previous = _parse(claim["last_decayed_at"]) if claim.get("last_decayed_at") else None
            if previous is not None and previous.replace(hour=0, minute=0, second=0, microsecond=0) >= day_start:
                continue
            anchor = _parse(str(claim.get("last_accessed_at") or claim["recorded_from"]))
            half_life = half_lives.get(_halflife_scope(claim), permanent_half_life_days)
            below_since = _parse(claim["decay_below_since"]) if claim.get("decay_below_since") else None
            if model == "activation_halflife":
                inactive_days = (reference - anchor).total_seconds() / 86400.0
                value = activation_at(
                    float(claim.get("activation_base") or 0.0),
                    inactive_days=inactive_days,
                    half_life_days=half_life,
                )
                if value <= archive_threshold and below_since is None:
                    base = max(float(claim.get("activation_base") or 0.0), archive_threshold)
                    crossing_days = half_life * math.log2(base / archive_threshold)
                    below_since = anchor + timedelta(days=crossing_days)
                field = "activation"
            else:
                elapsed_from = max(anchor, previous) if previous is not None else anchor
                elapsed_days = max(0.0, (day_start - elapsed_from).total_seconds() / 86400.0)
                value = activation_at(
                    float(claim.get("confidence") or 0.0),
                    inactive_days=elapsed_days,
                    half_life_days=half_life,
                )
                if value <= archive_threshold and below_since is None:
                    below_since = day_start
                field = "confidence"
            if value > archive_threshold:
                below_since = None
            should_archive = below_since is not None and reference >= below_since + timedelta(days=archive_grace_days)
            if should_archive:
                assert below_since is not None
                assert_transition(str(claim["status"]), "archived")
                cursor = connection.execute(
                    f"UPDATE claims SET {field}=?,decay_below_since=?,last_decayed_at=?,"
                    "status='archived',embedding_dense=NULL,embedding_sparse=NULL "
                    "WHERE id=? AND status IN ('active','disputed')",
                    (value, below_since.isoformat(), day_start.isoformat(), claim["id"]),
                )
                if cursor.rowcount == 1:
                    repository.delete_vector(str(claim["id"]))
                archived += cursor.rowcount
                continue
            cursor = connection.execute(
                f"UPDATE claims SET {field}=?,decay_below_since=?,last_decayed_at=? "
                "WHERE id=? AND status IN ('active','disputed')",
                (
                    value,
                    below_since.isoformat() if below_since is not None else None,
                    day_start.isoformat(),
                    claim["id"],
                ),
            )
            decayed += cursor.rowcount
        connection.commit()
        return {"decayed": decayed, "archived": archived}
    except Exception:
        connection.rollback()
        raise


def cleanup_stale_temporal_claims(
    connection: sqlite3.Connection,
    now: str | None = None,
    *,
    age_days: int,
    expiry_days: int,
) -> dict[str, int]:
    """在写事务快照内保守清理缺少 expires_at 的陈旧 temporal Claim。"""
    reference = _parse(now) if now else datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=age_days)
    promoted = 0
    expired_at_set = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT id,recorded_from,canonical_attribute FROM claims "
            "WHERE scope=? AND expires_at IS NULL AND status=? AND recorded_from<?",
            ("temporal", "active", cutoff.isoformat()),
        ).fetchall()
        for row in rows:
            recorded_from = _parse(row["recorded_from"])
            attribute = str(row["canonical_attribute"] or "")
            if attribute.startswith(("fact.decision", "fact.history", "fact.architecture")):
                cursor = connection.execute(
                    "UPDATE claims SET scope=? WHERE id=? AND scope=? AND expires_at IS NULL AND status=? "
                    "AND canonical_attribute IS ? AND recorded_from=?",
                    (
                        "permanent",
                        row["id"],
                        "temporal",
                        "active",
                        row["canonical_attribute"],
                        row["recorded_from"],
                    ),
                )
                promoted += cursor.rowcount
            elif attribute.startswith(("state.", "plan.", "config.env")):
                expires_at = (recorded_from + timedelta(days=expiry_days)).isoformat()
                cursor = connection.execute(
                    "UPDATE claims SET expires_at=? "
                    "WHERE id=? AND scope=? AND expires_at IS NULL AND status=? "
                    "AND canonical_attribute IS ? AND recorded_from=?",
                    (
                        expires_at,
                        row["id"],
                        "temporal",
                        "active",
                        row["canonical_attribute"],
                        row["recorded_from"],
                    ),
                )
                expired_at_set += cursor.rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {"expired_at_set": expired_at_set, "promoted": promoted}


def decay_claims(
    connection: sqlite3.Connection,
    now: str | None = None,
    *,
    temporal_decay_days: int,
    temporal_archive_days: int,
    permanent_decay_days: int,
    permanent_archive_days: int,
    access_bonus_every: int,
    access_bonus_days: int,
    access_bonus_cap_days: int,
    rollout_grace_days: int,
    min_confidence: float,
    feedback_lifecycle_mode: str,
    feedback_bonus_cap_days: int,
    decay_model: str = "legacy_linear",
    temporal_half_life_days: int = 45,
    permanent_half_life_days: int = 90,
    identity_half_life_days: int = 365,
    halflife_archive_threshold: float = 0.05,
    halflife_archive_grace_days: int = 7,
) -> dict[str, int]:
    """Linearly decay inactive claims and archive them at scope-specific boundaries."""
    reference = _parse(now) if now else datetime.now(timezone.utc)
    if decay_model not in {"legacy_linear", "activation_halflife", "confidence_halflife"}:
        raise ValueError(f"unsupported decay model: {decay_model}")
    if decay_model != "legacy_linear":
        return _decay_halflife(
            connection,
            reference,
            model=decay_model,
            temporal_half_life_days=temporal_half_life_days,
            permanent_half_life_days=permanent_half_life_days,
            identity_half_life_days=identity_half_life_days,
            archive_threshold=halflife_archive_threshold,
            archive_grace_days=halflife_archive_grace_days,
        )
    day_start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    minimum = min(1.0, max(0.0, float(min_confidence)))
    policy = {
        "temporal": (temporal_decay_days, temporal_archive_days),
        "permanent": (permanent_decay_days, permanent_archive_days),
    }
    repository = ClaimRepository(connection)
    decayed = archived = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        migration = connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE version='005_memory_management'"
        ).fetchone()
        migration_at = _parse(migration[0]) if migration else None
        grace_until = migration_at + timedelta(days=rollout_grace_days) if migration_at else None
        rows = connection.execute(
            "SELECT c.id,c.scope,c.confidence,c.access_count,c.recorded_from,c.last_accessed_at,c.last_decayed_at,"
            "c.expires_at,c.status,c.canonical_attribute,COALESCE(u.retention_bonus_days,0) AS feedback_bonus "
            "FROM claims c LEFT JOIN memory_usefulness u ON u.memory_type='claim' AND u.memory_id=c.id "
            "WHERE c.status IN ('active','disputed')"
        ).fetchall()
        for row in rows:
            claim = dict(row)
            if claim["status"] == "active" and is_protected_attribute(claim.get("canonical_attribute")):
                continue
            anchor = _parse(claim["last_accessed_at"] or claim["recorded_from"])
            if (
                claim["last_accessed_at"] is None
                and migration_at
                and grace_until
                and reference < grace_until
                and anchor <= migration_at
            ):
                continue
            decay_after, archive_after = policy.get(claim["scope"], policy["permanent"])
            access_count = max(0, int(claim.get("access_count") or 0))
            bonus = (
                0
                if claim.get("expires_at")
                else min(
                    access_count // access_bonus_every * access_bonus_days,
                    access_bonus_cap_days,
                )
            )
            decay_after += bonus
            archive_after += bonus
            if feedback_lifecycle_mode == "on":
                feedback_bonus = min(
                    int(claim.get("feedback_bonus") or 0),
                    feedback_bonus_cap_days,
                )
                decay_after += feedback_bonus
                archive_after += feedback_bonus
            inactive_days = (reference - anchor).total_seconds() / 86400.0
            if inactive_days > archive_after:
                assert_transition(claim["status"], "archived")
                cursor = connection.execute(
                    "UPDATE claims SET status='archived',embedding_dense=NULL,embedding_sparse=NULL "
                    "WHERE id=? AND status IN ('active','disputed')",
                    (claim["id"],),
                )
                if cursor.rowcount == 1:
                    repository.delete_vector(str(claim["id"]))
                archived += cursor.rowcount
                continue
            if inactive_days <= decay_after:
                continue
            previous = _parse(claim["last_decayed_at"]) if claim["last_decayed_at"] else None
            if previous is not None:
                previous = previous.replace(hour=0, minute=0, second=0, microsecond=0)
            if previous is not None and previous >= day_start:
                continue
            decay_start = anchor + timedelta(days=decay_after)
            elapsed_from = max(decay_start, previous) if previous else decay_start
            elapsed_days = int((day_start - elapsed_from).total_seconds() // 86400)
            if elapsed_days <= 0:
                continue
            daily_delta = (1.0 - minimum) / (archive_after - decay_after)
            confidence = max(minimum, float(claim["confidence"] or 0.0) - daily_delta * elapsed_days)
            cursor = connection.execute(
                "UPDATE claims SET confidence=?,last_decayed_at=? " "WHERE id=? AND status IN ('active','disputed')",
                (confidence, day_start.isoformat(), claim["id"]),
            )
            decayed += cursor.rowcount
        connection.commit()
        return {"decayed": decayed, "archived": archived}
    except Exception:
        connection.rollback()
        raise
