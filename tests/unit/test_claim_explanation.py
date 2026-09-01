"""Read-only, bounded Claim provenance explanations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.application.claim_explanation import explain_claim
from hl_mem.errors import NotFoundError
from hl_mem.storage.database import Database

NOW = "2026-09-01T00:00:00+00:00"


def _connection(tmp_path: Path) -> sqlite3.Connection:
    database = Database(tmp_path / "explain.db")
    connection = database.open()
    connection.execute(
        "INSERT INTO claims(id,namespace_key,predicate,value_json,recorded_from,status,source_authority,"
        "assertion_kind,scope,expires_at,superseded_by_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "claim-1",
            "default",
            "fact",
            '"private claim body"',
            NOW,
            "active",
            "low",
            "observation",
            "temporal",
            None,
            None,
        ),
    )
    return connection


def _event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    origin: str,
    session: str,
    source_uri: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at,source_uri,metadata_json,"
        "origin_class,session_kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            "tool_result",
            "tool",
            '{"body":"raw-private-tool-output"}',
            NOW,
            NOW,
            source_uri,
            '{"authorization":"super-secret","oversized":"' + ("x" * 2000) + '"}',
            origin,
            session,
        ),
    )
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
        "VALUES(?,?,?,?,?,?,?)",
        (f"link-{event_id}", "claim", "claim-1", "event", event_id, "derived_from", 1.0),
    )
    connection.commit()


def test_external_explanation_is_current_bounded_and_redacted(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _event(
        connection,
        "event-1",
        origin="external_derived",
        session="interactive",
        source_uri="https://user:password@example.com/private?token=secret#fragment",
    )

    result = explain_claim(connection, "claim-1", provenance_mode="enforce")

    assert result["schema_version"] == 1
    assert result["explanation_kind"] == "current_persisted_state"
    assert result["claim"] == {
        "id": "claim-1",
        "status": "active",
        "source_authority": "low",
        "assertion_kind": "observation",
        "scope": "temporal",
        "recorded_from": NOW,
        "recorded_to": None,
        "valid_from": None,
        "valid_to": None,
        "expires_at": None,
        "supersedes_id": None,
        "superseded_by_id": None,
    }
    assert result["provenance"] == {
        "mode": "enforce",
        "origin_class": "external_derived",
        "session_kind": "interactive",
        "external": True,
        "automated": False,
        "evidence_count": 1,
        "missing_evidence_count": 0,
        "interpretation": "restricted_source",
    }
    assert result["evidence"][0]["event"]["source_hint"] == "https://example.com"
    rendered = repr(result)
    for private in ("private claim body", "raw-private-tool-output", "super-secret", "password", "token=", "fragment"):
        assert private not in rendered
    assert result["limitations"] == ["current_state_only", "not_historical_admission_reconstruction"]


@pytest.mark.parametrize("status", ["superseded", "expired"])
def test_explanation_reports_persisted_lifecycle_state(tmp_path: Path, status: str) -> None:
    connection = _connection(tmp_path)
    if status == "superseded":
        connection.execute(
            "INSERT INTO claims(id,namespace_key,value_json,recorded_from,status) "
            "VALUES('claim-2','default','null',?,'active')",
            (NOW,),
        )
    connection.execute(
        "UPDATE claims SET status=?,recorded_to=?,valid_to=?,superseded_by_id=? WHERE id='claim-1'",
        (status, NOW, NOW, "claim-2" if status == "superseded" else None),
    )
    connection.commit()

    result = explain_claim(connection, "claim-1")

    assert result["claim"]["status"] == status
    assert result["claim"]["recorded_to"] == NOW
    assert result["claim"]["valid_to"] == NOW


def test_mixed_and_dangling_evidence_use_conservative_safe_summary(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _event(connection, "direct", origin="direct_user", session="interactive")
    _event(connection, "cron", origin="system", session="cron", source_uri="not a valid uri SECRET")
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
        "VALUES('dangling','claim','claim-1','event','missing-event','supports',1.0)"
    )
    connection.commit()

    result = explain_claim(connection, "claim-1")

    assert result["provenance"]["origin_class"] == "system"
    assert result["provenance"]["session_kind"] == "cron"
    assert result["provenance"]["automated"] is True
    assert result["provenance"]["missing_evidence_count"] == 1
    dangling = next(item for item in result["evidence"] if item["id"] == "missing-event")
    assert dangling == {
        "type": "event",
        "id": "missing-event",
        "relation": "supports",
        "weight": 1.0,
        "event": None,
        "missing": True,
    }
    assert "SECRET" not in repr(result)


def test_unknown_and_non_event_evidence_preserve_legacy_interpretation(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _event(connection, "legacy", origin="unknown", session="unknown")
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation,weight) "
        "VALUES('claim-link','claim','claim-1','claim','parent-claim','derived_from',0.5)"
    )
    connection.commit()

    result = explain_claim(connection, "claim-1", provenance_mode="observe")

    assert result["provenance"]["interpretation"] == "observe_only"
    assert result["provenance"]["origin_class"] == "unknown"
    non_event = next(item for item in result["evidence"] if item["type"] == "claim")
    assert non_event["event"] is None
    assert non_event["missing"] is False


def test_missing_claim_raises_not_found(tmp_path: Path) -> None:
    connection = _connection(tmp_path)

    with pytest.raises(NotFoundError, match="claim not found"):
        explain_claim(connection, "absent")
