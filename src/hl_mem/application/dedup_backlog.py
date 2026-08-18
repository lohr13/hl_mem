"""Fail-closed deterministic drain for pending pairs below the active dedup floor."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from hl_mem.domain.claims.dedup import DEDUP_POLICY_VERSION
from hl_mem.errors import ConflictError

BELOW_FLOOR_DECISION = "dismissed_below_floor"
BELOW_FLOOR_REASON = f"{DEDUP_POLICY_VERSION}_below_current_floor"


def _validate_threshold(threshold: float) -> None:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("dedup threshold must be between 0 and 1")


def inspect_below_floor_pairs(
    connection: sqlite3.Connection,
    *,
    threshold: float,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Return a bounded, content-free report without mutating the database."""
    _validate_threshold(threshold)
    if sample_limit < 0 or sample_limit > 100:
        raise ValueError("sample_limit must be between 0 and 100")
    summary = connection.execute(
        "SELECT count(*) AS item_count,min(similarity) AS minimum,max(similarity) AS maximum "
        "FROM dedup_pairs WHERE decision IS NULL AND similarity<?",
        (threshold,),
    ).fetchone()
    namespace_rows = connection.execute(
        "SELECT namespace_key,count(*) AS item_count FROM dedup_pairs "
        "WHERE decision IS NULL AND similarity<? GROUP BY namespace_key ORDER BY namespace_key",
        (threshold,),
    ).fetchall()
    source_rows = connection.execute(
        "SELECT pair_source,count(*) AS item_count FROM dedup_pairs "
        "WHERE decision IS NULL AND similarity<? GROUP BY pair_source ORDER BY pair_source",
        (threshold,),
    ).fetchall()
    sample_rows = connection.execute(
        "SELECT id FROM dedup_pairs WHERE decision IS NULL AND similarity<? ORDER BY id LIMIT ?",
        (threshold, sample_limit),
    ).fetchall()
    candidate_count = int(summary["item_count"])
    return {
        "threshold": threshold,
        "policy_version": DEDUP_POLICY_VERSION,
        "terminal_decision": BELOW_FLOOR_DECISION,
        "judge_reason": BELOW_FLOOR_REASON,
        "candidate_pair_count": candidate_count,
        "similarity_min": float(summary["minimum"]) if summary["minimum"] is not None else None,
        "similarity_max": float(summary["maximum"]) if summary["maximum"] is not None else None,
        "namespace_counts": {str(row["namespace_key"]): int(row["item_count"]) for row in namespace_rows},
        "pair_source_counts": {str(row["pair_source"]): int(row["item_count"]) for row in source_rows},
        "sample_pair_ids": [str(row["id"]) for row in sample_rows],
        "sample_truncated": candidate_count > sample_limit,
        "pending_pair_count": int(
            connection.execute("SELECT count(*) FROM dedup_pairs WHERE decision IS NULL").fetchone()[0]
        ),
    }


def drain_below_floor_pairs(
    connection: sqlite3.Connection,
    *,
    threshold: float,
    expected_count: int,
    reviewed_at: str | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Terminally classify the exact below-floor set in one fail-closed transaction."""
    _validate_threshold(threshold)
    if expected_count < 0:
        raise ValueError("expected_count must not be negative")
    if connection.in_transaction:
        raise ConflictError("dedup backlog drain requires a clean connection")
    timestamp = reviewed_at or datetime.now(timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        preview = inspect_below_floor_pairs(connection, threshold=threshold)
        actual_count = int(preview["candidate_pair_count"])
        if actual_count != expected_count:
            raise ConflictError(f"dedup below-floor count mismatch: expected {expected_count}, found {actual_count}")
        connection.execute("DROP TABLE IF EXISTS temp.dedup_below_floor_targets")
        connection.execute(
            "CREATE TEMP TABLE dedup_below_floor_targets AS "
            "SELECT id FROM dedup_pairs WHERE decision IS NULL AND similarity<?",
            (threshold,),
        )
        selected_count = int(connection.execute("SELECT count(*) FROM dedup_below_floor_targets").fetchone()[0])
        if selected_count != expected_count:
            raise ConflictError(
                f"dedup below-floor target changed during drain: expected {expected_count}, found {selected_count}"
            )
        updated = connection.execute(
            "UPDATE dedup_pairs SET decision=?,policy_version=?,judge_confidence=NULL,judge_reason=?,"
            "judge_model=NULL,reviewed_at=? "
            "WHERE id IN (SELECT id FROM dedup_below_floor_targets) "
            "AND decision IS NULL AND similarity<?",
            (
                BELOW_FLOOR_DECISION,
                DEDUP_POLICY_VERSION,
                BELOW_FLOOR_REASON,
                timestamp,
                threshold,
            ),
        )
        if updated.rowcount != expected_count:
            raise ConflictError(
                f"dedup below-floor CAS mismatch: expected {expected_count}, updated {updated.rowcount}"
            )
        remaining = int(
            connection.execute(
                "SELECT count(*) FROM dedup_pairs WHERE decision IS NULL AND similarity<?",
                (threshold,),
            ).fetchone()[0]
        )
        if remaining:
            raise ConflictError(f"dedup below-floor pairs remain pending after drain: {remaining}")
        detail = {
            "item": "drain_dedup_below_floor",
            "source": source,
            "threshold": threshold,
            "judge_reason": BELOW_FLOOR_REASON,
            "applied_pair_count": int(updated.rowcount),
        }
        connection.execute(
            "INSERT INTO audit_log(occurred_at,phase,action,outcome,trace_id,detail_json) "
            "VALUES (?,'maintenance','drain_dedup_below_floor','success',?,?)",
            (timestamp, uuid.uuid4().hex, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )
        connection.execute("DROP TABLE dedup_below_floor_targets")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {
        **preview,
        "dry_run": False,
        "applied_pair_count": int(updated.rowcount),
        "remaining_below_floor_count": remaining,
        "claim_rows_updated": 0,
    }
