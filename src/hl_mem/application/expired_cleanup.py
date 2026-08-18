"""Bounded, fail-closed reclamation of expired Claim history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from hl_mem.application.conflicts import OPEN_CASE_STATUSES
from hl_mem.application.deletion import DeletionRejectedError, DeletionService
from hl_mem.errors import ConflictError

ExpiredCleanupMode = Literal["off", "observe", "on"]


def _reference_time(now: str) -> datetime:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("expired cleanup now must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate(retention_days: int, sample_limit: int = 20) -> None:
    if retention_days < 1:
        raise ValueError("expired cleanup retention_days must be positive")
    if not 0 <= sample_limit <= 100:
        raise ValueError("sample_limit must be between 0 and 100")


def _anchor_sql(alias: str = "c") -> str:
    return f"COALESCE({alias}.valid_to,{alias}.expires_at,{alias}.recorded_from)"


def _has_consumer_sql(alias: str = "c") -> str:
    return (
        "EXISTS (SELECT 1 FROM evidence_links AS consumer "
        f"WHERE consumer.evidence_type='claim' AND consumer.evidence_id={alias}.id)"
    )


def _has_open_conflict_sql(alias: str = "c") -> str:
    placeholders = ",".join(f"'{status}'" for status in sorted(OPEN_CASE_STATUSES))
    return (
        "EXISTS (SELECT 1 FROM conflict_cases AS conflict "
        f"WHERE (conflict.left_claim_id={alias}.id OR conflict.right_claim_id={alias}.id) "
        f"AND conflict.status IN ({placeholders}))"
    )


def _eligibility_sql(alias: str = "c") -> str:
    return (
        f"{alias}.status='expired' AND {_anchor_sql(alias)}<=? "
        f"AND NOT {_has_consumer_sql(alias)} AND NOT {_has_open_conflict_sql(alias)}"
    )


def inspect_expired_claims(
    connection: sqlite3.Connection,
    *,
    now: str,
    retention_days: int,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Explain the mutually exclusive eligibility boundary without writing."""
    _validate(retention_days, sample_limit)
    reference = _reference_time(now)
    cutoff = (reference - timedelta(days=retention_days)).isoformat()
    anchor = _anchor_sql()
    consumer = _has_consumer_sql()
    open_conflict = _has_open_conflict_sql()
    counts = connection.execute(
        "SELECT count(*) AS expired_count,"
        f"sum(CASE WHEN {anchor}>? THEN 1 ELSE 0 END) AS too_recent_count,"
        f"sum(CASE WHEN {anchor}<=? AND {consumer} THEN 1 ELSE 0 END) AS consumer_count,"
        f"sum(CASE WHEN {anchor}<=? AND NOT {consumer} AND {open_conflict} THEN 1 ELSE 0 END) "
        "AS conflict_count,"
        f"sum(CASE WHEN {_eligibility_sql()} THEN 1 ELSE 0 END) AS eligible_count "
        "FROM claims c WHERE c.status='expired'",
        (cutoff, cutoff, cutoff, cutoff),
    ).fetchone()
    eligible_count = int(counts["eligible_count"] or 0)
    sample = connection.execute(
        f"SELECT c.id FROM claims c WHERE {_eligibility_sql()} " f"ORDER BY {_anchor_sql()},c.id LIMIT ?",
        (cutoff, sample_limit),
    ).fetchall()
    return {
        "as_of": reference.isoformat(),
        "retention_days": retention_days,
        "cutoff": cutoff,
        "expired_claim_count": int(counts["expired_count"] or 0),
        "eligible_claim_count": eligible_count,
        "too_recent_count": int(counts["too_recent_count"] or 0),
        "evidence_consumer_count": int(counts["consumer_count"] or 0),
        "open_conflict_count": int(counts["conflict_count"] or 0),
        "sample_eligible_claim_ids": [str(row[0]) for row in sample],
        "sample_truncated": eligible_count > sample_limit,
    }


def cleanup_expired_claims(
    connection: sqlite3.Connection,
    *,
    now: str,
    retention_days: int,
    batch_size: int,
    expected_count: int,
    ledger_path: str | Path | None = None,
    source: str = "maintenance",
) -> dict[str, Any]:
    """Delete at most one bounded batch after validating the full eligible count."""
    _validate(retention_days)
    if batch_size < 1:
        raise ValueError("expired cleanup batch_size must be positive")
    if expected_count < 0:
        raise ValueError("expected_count must not be negative")
    if connection.in_transaction:
        raise ConflictError("expired cleanup requires a clean connection")
    reference = _reference_time(now)
    cutoff = (reference - timedelta(days=retention_days)).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        preview = inspect_expired_claims(
            connection,
            now=reference.isoformat(),
            retention_days=retention_days,
        )
        actual_count = int(preview["eligible_claim_count"])
        if actual_count != expected_count:
            raise ConflictError(f"expired cleanup count mismatch: expected {expected_count}, found {actual_count}")
        claim_ids = [
            str(row[0])
            for row in connection.execute(
                f"SELECT c.id FROM claims c WHERE {_eligibility_sql()} " f"ORDER BY {_anchor_sql()},c.id LIMIT ?",
                (cutoff, batch_size),
            ).fetchall()
        ]
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise

    service = DeletionService(connection, ledger_path=ledger_path)
    deleted = 0
    rejections: dict[str, str] = {}
    for claim_id in claim_ids:
        try:
            result = service.delete_expired_claim(claim_id)
            deleted += int(result.deleted)
        except DeletionRejectedError as error:
            rejections[claim_id] = error.reason
    remaining = inspect_expired_claims(
        connection,
        now=reference.isoformat(),
        retention_days=retention_days,
    )
    return {
        **preview,
        "dry_run": False,
        "source": source,
        "expected_count": expected_count,
        "batch_size": batch_size,
        "scanned": len(claim_ids),
        "deleted": deleted,
        "rejected": len(rejections),
        "rejections": rejections,
        "remaining_eligible_count": int(remaining["eligible_claim_count"]),
    }


def maintain_expired_claims(
    connection: sqlite3.Connection,
    *,
    now: str,
    retention_days: int,
    batch_size: int,
    mode: ExpiredCleanupMode,
) -> dict[str, Any]:
    """Maintenance adapter: default observe; on snapshots its exact expected count."""
    if mode not in {"off", "observe", "on"}:
        raise ValueError("expired cleanup mode must be off, observe, or on")
    preview = inspect_expired_claims(connection, now=now, retention_days=retention_days)
    if mode != "on":
        return {
            **preview,
            "mode": mode,
            "dry_run": True,
            "deleted": 0,
            "remaining_eligible_count": int(preview["eligible_claim_count"]),
        }
    return {
        "mode": mode,
        **cleanup_expired_claims(
            connection,
            now=now,
            retention_days=retention_days,
            batch_size=batch_size,
            expected_count=int(preview["eligible_claim_count"]),
            source="maintenance",
        ),
    }
