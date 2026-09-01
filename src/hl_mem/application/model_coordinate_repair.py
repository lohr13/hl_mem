"""Bounded repair of source-proven operational-model history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.conflict_queries import OPEN_CASE_STATUSES
from hl_mem.errors import ConflictError
from hl_mem.ingest.extraction.model_coordinates import project_model_coordinates
from hl_mem.storage._shared import decode_json
from hl_mem.storage.claims import ClaimRepository

_AUTHORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _winner_rows(connection: sqlite3.Connection, namespace: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id,value_json,valid_from,observed_at,recorded_from,source_authority FROM claims "
        "WHERE namespace_key=? AND status='active' "
        "AND subject_canonical_entity_id='project:hl_mem' AND canonical_slot='choice.model' "
        "AND json_extract(qualifiers_json,'$.task')='extraction' "
        "AND json_extract(qualifiers_json,'$.runtime_config')=1 "
        "ORDER BY recorded_from DESC,id DESC",
        (namespace,),
    ).fetchall()


def _event_texts(connection: sqlite3.Connection, claim_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT event.content_json FROM evidence_links AS link JOIN events AS event "
        "ON event.id=link.evidence_id WHERE link.derived_type='claim' AND link.derived_id=? "
        "AND link.evidence_type='event' ORDER BY event.recorded_at,event.id",
        (claim_id,),
    ).fetchall()
    texts: list[str] = []
    for row in rows:
        try:
            content = json.loads(str(row[0]))
        except (TypeError, ValueError):
            continue
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            texts.append(content["text"])
        elif isinstance(content, str):
            texts.append(content)
    return tuple(texts)


def _has_open_conflict(connection: sqlite3.Connection, claim_id: str) -> bool:
    placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
    row = connection.execute(
        "SELECT 1 FROM conflict_cases WHERE (left_claim_id=? OR right_claim_id=?) "
        f"AND status IN ({placeholders}) LIMIT 1",
        (claim_id, claim_id, *OPEN_CASE_STATUSES),
    ).fetchone()
    return row is not None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _is_older(candidate: sqlite3.Row, winner: sqlite3.Row) -> bool:
    candidate_recorded = _timestamp(candidate["recorded_from"])
    winner_recorded = _timestamp(winner["recorded_from"])
    candidate_valid = _timestamp(candidate["valid_from"] or candidate["observed_at"] or candidate["recorded_from"])
    winner_valid = _timestamp(winner["valid_from"] or winner["observed_at"] or winner["recorded_from"])
    if candidate_recorded is None or winner_recorded is None or candidate_valid is None or winner_valid is None:
        return False
    return candidate_recorded < winner_recorded and candidate_valid <= winner_valid


def _source_proves_extraction(connection: sqlite3.Connection, candidate: sqlite3.Row) -> bool:
    value = decode_json(candidate["value_json"])
    if not isinstance(value, str):
        return False
    subject = str(candidate["subject_entity_id"] or "")
    for evidence_text in _event_texts(connection, str(candidate["id"])):
        projection = project_model_coordinates("choice.model", subject, value, evidence_text)
        if projection.subject == "hl_mem" and projection.task == "extraction" and projection.state_change:
            return True
    return False


def _blocked_preview(namespace: str, winner_count: int) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blocker": f"authoritative_winner_count:{winner_count}",
        "namespace": namespace,
        "dry_run": True,
        "winner_claim_id": None,
        "candidate_claim_count": 0,
        "candidate_claim_ids": [],
        "excluded_claim_ids": [],
    }


def inspect_model_coordinate_history(
    connection: sqlite3.Connection,
    *,
    namespace: str = "default",
) -> dict[str, Any]:
    """Return the exact historical repair set without writing."""
    winners = _winner_rows(connection, namespace)
    if len(winners) != 1:
        return _blocked_preview(namespace, len(winners))
    winner = winners[0]
    rows = connection.execute(
        "SELECT id,subject_entity_id,value_json,valid_from,observed_at,recorded_from,source_authority FROM claims "
        "WHERE namespace_key=? AND status='active' AND canonical_attribute='choice.model' "
        "AND id<>? AND COALESCE(json_extract(qualifiers_json,'$.runtime_config'),0)<>1 "
        "AND (canonical_slot IS NULL OR conflict_key IS NULL OR subject_canonical_entity_id IS NULL) "
        "ORDER BY recorded_from,id",
        (namespace, winner["id"]),
    ).fetchall()
    winner_authority = _AUTHORITY_RANK.get(str(winner["source_authority"] or "medium"), 1)
    candidates: list[str] = []
    excluded: list[str] = []
    for row in rows:
        claim_id = str(row["id"])
        candidate_authority = _AUTHORITY_RANK.get(str(row["source_authority"] or "medium"), 1)
        eligible = (
            _is_older(row, winner)
            and candidate_authority <= winner_authority
            and not _has_open_conflict(connection, claim_id)
            and _source_proves_extraction(connection, row)
        )
        (candidates if eligible else excluded).append(claim_id)
    return {
        "status": "ready",
        "blocker": None,
        "namespace": namespace,
        "dry_run": True,
        "winner_claim_id": str(winner["id"]),
        "candidate_claim_count": len(candidates),
        "candidate_claim_ids": sorted(candidates),
        "excluded_claim_ids": sorted(excluded),
    }


def apply_model_coordinate_history_repair(
    connection: sqlite3.Connection,
    *,
    expected_count: int,
    namespace: str = "default",
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Supersede the exact inspected set in one count-guarded transaction."""
    if expected_count < 0:
        raise ValueError("expected_count must not be negative")
    if connection.in_transaction:
        raise ConflictError("model coordinate repair requires a clean connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        preview = inspect_model_coordinate_history(connection, namespace=namespace)
        if preview["status"] != "ready":
            raise ConflictError("model coordinate repair requires exactly one authoritative winner")
        actual_count = int(preview["candidate_claim_count"])
        if actual_count != expected_count:
            raise ConflictError(
                f"model coordinate repair count mismatch: expected {expected_count}, found {actual_count}"
            )
        winner_id = str(preview["winner_claim_id"])
        winner = connection.execute(
            "SELECT value_json,valid_from,observed_at,recorded_from FROM claims WHERE id=?",
            (winner_id,),
        ).fetchone()
        if winner is None:
            raise ConflictError("authoritative winner disappeared during repair")
        winner_value = decode_json(winner["value_json"])
        changed_at = str(winner["valid_from"] or winner["observed_at"] or winner["recorded_from"])
        mutation_time = recorded_at or datetime.now(timezone.utc).isoformat()
        repository = ClaimRepository(connection)
        for claim_id in preview["candidate_claim_ids"]:
            result = repository.supersede_with_inline(
                str(claim_id),
                winner_id,
                winner_value,
                changed_at,
                mutation_time,
                commit=False,
            )
            if not result.applied:
                raise ConflictError(f"model coordinate repair compare-and-set failed: {claim_id}")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {
        **preview,
        "dry_run": False,
        "expected_count": expected_count,
        "applied_claim_count": actual_count,
    }
