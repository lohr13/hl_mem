"""Retention anchors for durable and episodic extracted claims."""

from __future__ import annotations

import uuid
from pathlib import Path

from hl_mem.application.ingest import IngestService
from hl_mem.domain.claims.retention import TTLPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


def _store_times(
    tmp_path: Path,
    claim: ExtractedClaim,
    *,
    event_time: str,
    recorded_from: str,
) -> tuple[str | None, str, str]:
    database = Database(tmp_path / f"{uuid.uuid4().hex}.db")
    connection = database.open()
    event = {
        "id": uuid.uuid4().hex,
        "tenant_id": "default",
        "actor_type": "user",
        "event_type": "message",
        "content": {"text": claim.value},
        "occurred_at": event_time,
        "recorded_at": recorded_from,
    }
    EventRepository(connection).insert_event(event)
    result = IngestService.store_extracted(
        connection,
        claim,
        event,
        recorded_from,
        FakeEmbedder(8),
        policy=TTLPolicy(temporal_ttl_days_low=3, temporal_ttl_days_normal=7, temporal_ttl_days_high=14),
    )
    row = connection.execute(
        "SELECT expires_at,valid_from,observed_at FROM claims WHERE id=?",
        (result.claim_id,),
    ).fetchone()
    database.close()
    return row["expires_at"], row["valid_from"], row["observed_at"]


def test_durable_historical_plan_anchors_at_recording_time(tmp_path: Path) -> None:
    times = _store_times(
        tmp_path,
        ExtractedClaim("plan", "Renew passport", canonical_attribute="plan.other", scope="temporal", importance=0.6),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )

    assert times == ("2026-09-10T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00")


def test_durable_future_plan_anchors_after_latest_occurrence_boundary(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim(
            "plan",
            "Visit the office from 9 to 18 on September 11",
            canonical_attribute="plan.other",
            scope="temporal",
            importance=0.6,
            occurred_start="2026-09-11T09:00:00+08:00",
            occurred_end="2026-09-11T18:00:00+08:00",
        ),
        event_time="2026-09-03T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )

    assert expires_at == "2026-09-18T10:00:00+00:00"


def test_durable_temporal_non_plan_keeps_event_anchor(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim(
            "fact", "The user changed jobs", canonical_attribute="fact.other", scope="temporal", importance=0.6
        ),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )

    assert expires_at == "2024-01-08T00:00:00+00:00"


def test_episodic_non_plan_keeps_recording_anchor(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim(
            "fact",
            "The user assembled a bookcase",
            canonical_attribute="fact.other",
            memory_layer="episodic",
            scope="temporal",
            importance=0.3,
        ),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )

    assert expires_at == "2026-09-06T00:00:00+00:00"


def test_permanent_plan_remains_non_expiring(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim("plan", "Keep the plan", canonical_attribute="plan.other", scope="permanent", importance=0.8),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )

    assert expires_at is None
