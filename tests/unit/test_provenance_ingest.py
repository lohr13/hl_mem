"""Provenance-aware Claim admission tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

NOW = "2026-09-01T00:00:00+00:00"


def _source(
    connection,
    event_id: str,
    *,
    origin: str,
    session: str,
    event_type: str = "message",
    actor_type: str = "user",
) -> dict[str, object]:
    EventRepository(connection).insert_event(
        {
            "id": event_id,
            "event_type": event_type,
            "actor_type": actor_type,
            "content_json": "{}",
            "occurred_at": NOW,
            "recorded_at": NOW,
            "origin_class": origin,
            "session_kind": session,
        }
    )
    stored = EventRepository(connection).get_event(event_id)
    assert stored is not None
    return stored


def _store(
    tmp_path: Path,
    *,
    origin: str,
    session: str,
    assertion_kind: str = "unknown",
    scope: str = "permanent",
    event_type: str = "message",
    mode: str = "enforce",
) -> tuple[object, dict[str, object] | None]:
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / f"{origin}-{session}-{mode}.db"),
        provenance_mode=mode,
    )
    database = Database(settings=settings)
    connection = database.open()
    source = _source(
        connection,
        "event",
        origin=origin,
        session=session,
        event_type=event_type,
    )
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="fact",
            value=f"{origin}-{session}",
            subject="entity",
            assertion_kind=assertion_kind,
            scope=scope,
        ),
        source,
        NOW,
        FakeEmbedder(8),
        authority="high" if event_type == "explicit_memory" else None,
        policy=settings.retention_policy(),
        source_events=[source],
    )
    claim = (
        dict(connection.execute("SELECT * FROM claims WHERE id=?", (result.claim_id,)).fetchone())
        if result.claim_id is not None
        else None
    )
    database.close()
    return result, claim


@pytest.mark.parametrize(
    ("origin", "session"),
    [("external", "interactive"), ("external_derived", "interactive"), ("system", "cron")],
)
def test_restricted_sources_become_low_temporal_observations(
    tmp_path: Path,
    origin: str,
    session: str,
) -> None:
    result, claim = _store(tmp_path, origin=origin, session=session)

    assert result.status == "stored"
    assert claim is not None
    assert (claim["source_authority"], claim["assertion_kind"], claim["scope"]) == (
        "low",
        "observation",
        "temporal",
    )


def test_external_inference_stays_inference_and_explicit_memory_keeps_retention(tmp_path: Path) -> None:
    _result, inferred = _store(
        tmp_path,
        origin="external_derived",
        session="interactive",
        assertion_kind="inference",
    )
    _result, explicit = _store(
        tmp_path,
        origin="external",
        session="interactive",
        event_type="explicit_memory",
    )

    assert inferred is not None and inferred["assertion_kind"] == "inference"
    assert explicit is not None
    assert (explicit["source_authority"], explicit["scope"]) == ("low", "permanent")


@pytest.mark.parametrize("session", ["heartbeat", "subagent"])
def test_automated_sessions_cannot_bypass_claim_write_gate(tmp_path: Path, session: str) -> None:
    result, claim = _store(tmp_path, origin="system", session=session)

    assert result.status == "skipped"
    assert result.reason == f"blocked_{session}"
    assert claim is None


def test_mixed_evidence_uses_most_conservative_provenance(tmp_path: Path) -> None:
    settings = replace(Settings.for_test(), database_path=str(tmp_path / "mixed.db"), provenance_mode="enforce")
    database = Database(settings=settings)
    connection = database.open()
    direct = _source(connection, "direct", origin="direct_user", session="interactive")
    external = _source(connection, "external", origin="external", session="interactive")

    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(predicate="fact", value="mixed", subject="entity"),
        direct,
        NOW,
        FakeEmbedder(8),
        source_events=[direct, external],
    )
    claim = connection.execute(
        "SELECT source_authority,assertion_kind,scope FROM claims WHERE id=?",
        (result.claim_id,),
    ).fetchone()

    assert tuple(claim) == ("low", "observation", "temporal")
    database.close()


@pytest.mark.parametrize(
    ("origin", "session", "mode"),
    [("unknown", "unknown", "enforce"), ("external", "cron", "observe")],
)
def test_legacy_unknown_and_observe_preserve_existing_semantics(
    tmp_path: Path,
    origin: str,
    session: str,
    mode: str,
) -> None:
    _result, claim = _store(tmp_path, origin=origin, session=session, mode=mode)

    assert claim is not None
    assert (claim["source_authority"], claim["assertion_kind"], claim["scope"]) == (
        "medium",
        "unknown",
        "permanent",
    )
