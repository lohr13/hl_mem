"""Bounded, read-only explanation of persisted Claim provenance."""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlsplit

from hl_mem.domain.provenance import ProvenanceMode, aggregate_event_provenance
from hl_mem.errors import NotFoundError

_MAX_SOURCE_URI_LENGTH = 2048


def _source_hint(source_uri: object) -> str | None:
    """Reduce a locator to a bounded non-secret origin hint."""
    if not isinstance(source_uri, str) or not source_uri or len(source_uri) > _MAX_SOURCE_URI_LENGTH:
        return None
    try:
        parsed = urlsplit(source_uri)
        host = parsed.hostname
        if parsed.scheme in {"http", "https"} and host:
            safe_host = f"[{host}]" if ":" in host else host
            return f"{parsed.scheme}://{safe_host}"[:255]
        if parsed.scheme == "file":
            return "file"
    except (ValueError, UnicodeError):
        return None
    return None


def _interpret_policy(origin: str, session: str, mode: ProvenanceMode) -> str:
    if mode == "observe":
        return "observe_only"
    if session in {"heartbeat", "subagent"}:
        return "automated_session_blocked"
    if origin in {"external", "external_derived", "system"} or session == "cron":
        return "restricted_source"
    if origin == "unknown" and session == "unknown":
        return "legacy_unknown"
    return "standard"


def _claim_state(row: sqlite3.Row) -> dict[str, Any]:
    keys = (
        "id",
        "status",
        "source_authority",
        "assertion_kind",
        "scope",
        "recorded_from",
        "recorded_to",
        "valid_from",
        "valid_to",
        "expires_at",
        "supersedes_id",
        "superseded_by_id",
    )
    return {key: row[key] for key in keys}


def explain_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    provenance_mode: ProvenanceMode = "enforce",
) -> dict[str, Any]:
    """Explain current persisted state without reconstructing expired admission audit."""
    claim = connection.execute(
        "SELECT id,status,source_authority,assertion_kind,scope,recorded_from,recorded_to,"
        "valid_from,valid_to,expires_at,supersedes_id,superseded_by_id FROM claims WHERE id=?",
        (claim_id,),
    ).fetchone()
    if claim is None:
        raise NotFoundError(f"claim not found: {claim_id}")
    rows = connection.execute(
        "SELECT link.id AS link_id,link.evidence_type,link.evidence_id,link.relation,link.weight,"
        "event.id AS event_id,event.origin_class,event.session_kind,event.occurred_at,event.recorded_at,"
        "event.source_uri FROM evidence_links AS link LEFT JOIN events AS event "
        "ON link.evidence_type='event' AND event.id=link.evidence_id "
        "WHERE link.derived_type='claim' AND link.derived_id=? ORDER BY link.id",
        (claim_id,),
    ).fetchall()
    evidence: list[dict[str, Any]] = []
    event_sources: list[dict[str, Any]] = []
    missing_count = 0
    for row in rows:
        event = None
        missing = row["evidence_type"] == "event" and row["event_id"] is None
        if missing:
            missing_count += 1
        elif row["event_id"] is not None:
            event = {
                "id": row["event_id"],
                "origin_class": row["origin_class"],
                "session_kind": row["session_kind"],
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
                "source_hint": _source_hint(row["source_uri"]),
            }
            event_sources.append(event)
        evidence.append(
            {
                "type": row["evidence_type"],
                "id": row["evidence_id"],
                "relation": row["relation"],
                "weight": row["weight"],
                "event": event,
                "missing": missing,
            }
        )
    summary = aggregate_event_provenance(event_sources)
    provenance = {
        "mode": provenance_mode,
        "origin_class": summary.origin_class,
        "session_kind": summary.session_kind,
        "external": summary.external,
        "automated": summary.automated,
        "evidence_count": len(rows),
        "missing_evidence_count": missing_count,
        "interpretation": _interpret_policy(summary.origin_class, summary.session_kind, provenance_mode),
    }
    return {
        "schema_version": 1,
        "explanation_kind": "current_persisted_state",
        "claim": _claim_state(claim),
        "provenance": provenance,
        "evidence": evidence,
        "limitations": ["current_state_only", "not_historical_admission_reconstruction"],
    }
