from datetime import datetime, timezone

from hl_mem.application.ingest import IngestService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION, PROMPT_HASH
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


def test_llm_claim_records_bumped_extractor_version(tmp_path) -> None:
    connection = Database(tmp_path / "extractor-version.db").open()
    now = datetime.now(timezone.utc).isoformat()
    stored_event = {
        "id": "event-1",
        "event_type": "message",
        "actor_type": "user",
        "content": {"text": "hl_mem 使用 SQLite"},
        "occurred_at": now,
        "recorded_at": now,
    }
    EventRepository(connection).insert_event(stored_event)
    event = {
        **stored_event,
        "extractor": "llm",
        "extractor_version": LLM_EXTRACTOR_VERSION,
    }

    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            subject="hl_mem",
            predicate="使用",
            value="hl_mem 使用 SQLite",
            canonical_attribute="choice.database",
            importance=0.8,
        ),
        event,
        now,
        FakeEmbedder(8),
    )

    row = connection.execute(
        "SELECT extractor_version FROM claims WHERE id=?",
        (result.claim_id,),
    ).fetchone()
    assert row["extractor_version"] == LLM_EXTRACTOR_VERSION
    assert row["extractor_version"] == f"llm-v2+{PROMPT_HASH}"


def test_legacy_llm_extractor_version_remains_storable(tmp_path) -> None:
    connection = Database(tmp_path / "legacy-extractor-version.db").open()
    now = datetime.now(timezone.utc).isoformat()
    stored_event = {
        "id": "event-legacy",
        "event_type": "message",
        "actor_type": "user",
        "content": {"text": "hl_mem 使用 SQLite"},
        "occurred_at": now,
        "recorded_at": now,
    }
    EventRepository(connection).insert_event(stored_event)
    event = {
        **stored_event,
        "extractor": "llm",
        "extractor_version": "llm-v2",
    }

    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            subject="hl_mem",
            predicate="使用",
            value="hl_mem 使用 SQLite",
            canonical_attribute="choice.database",
            importance=0.8,
        ),
        event,
        now,
        FakeEmbedder(8),
    )

    row = connection.execute(
        "SELECT extractor_version FROM claims WHERE id=?",
        (result.claim_id,),
    ).fetchone()
    assert row["extractor_version"] == "llm-v2"
