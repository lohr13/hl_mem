from __future__ import annotations

from dataclasses import replace

from hl_mem.application.ingest import IngestService
from hl_mem.application.runtime_config_report import report_extraction_runtime
from hl_mem.domain.claims.conflicts import compute_conflict_key
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository

NOW = "2026-09-02T00:00:00+00:00"


def _settings(path, model: str = "glm-5.3-flash") -> Settings:
    return replace(
        Settings.for_test(),
        database_path=str(path),
        extractor_mode="llm",
        llm_provider="zhipu",
        llm_model=model,
        llm_base_url="https://example.invalid/v1",
        embedding_dim=8,
    )


def _event(event_id: str) -> dict[str, object]:
    return {
        "id": event_id,
        "tenant_id": "default",
        "event_type": "conversation",
        "actor_type": "user",
        "content": {"text": "HL-Mem 提取任务使用 qwen3.7-plus"},
        "occurred_at": NOW,
        "recorded_at": NOW,
        "origin_class": "direct_user",
        "session_kind": "interactive",
    }


def _seed_legacy_extraction_model(connection) -> str:
    event = _event("legacy-model-event")
    EventRepository(connection).insert_event(event)
    result = IngestService.store_extracted(
        connection,
        ExtractedClaim(
            predicate="使用",
            value="qwen3.7-plus",
            subject="hl_mem",
            qualifiers={"task": "extraction"},
            canonical_attribute="choice.model",
            canonical_slot="choice.model",
            assertion_kind="observation",
        ),
        event,
        NOW,
        FakeEmbedder(8),
        authority="high",
    )
    assert result.claim_id is not None
    legacy_key = compute_conflict_key(
        "default",
        "hl_mem",
        "使用",
        "choice.model",
        {"task": "extraction"},
        version=3,
    )
    connection.execute(
        "UPDATE claims SET subject_canonical_entity_id=NULL,conflict_key=?,conflict_key_version=3 WHERE id=?",
        (legacy_key, result.claim_id),
    )
    connection.execute("DELETE FROM claim_entity_links WHERE claim_id=?", (result.claim_id,))
    connection.commit()
    return result.claim_id


def test_runtime_report_supersedes_legacy_extraction_model_without_provider_calls(tmp_path) -> None:
    settings = _settings(tmp_path / "runtime.db")
    database = Database(settings=settings)
    connection = database.open()
    old_id = _seed_legacy_extraction_model(connection)

    report = report_extraction_runtime(connection, settings)

    assert report.stored is True
    assert report.reason == "stored"
    rows = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,value_json,status,subject_canonical_entity_id,canonical_slot,qualifiers_json "
            "FROM claims WHERE id IN (?,?)",
            (old_id, report.claim_id),
        ).fetchall()
    }
    assert rows[old_id]["status"] == "superseded"
    assert rows[report.claim_id]["status"] == "active"
    current = rows[report.claim_id]
    assert current["value_json"] == '"glm-5.3-flash"'
    assert current["subject_canonical_entity_id"] == "project:hl_mem"
    assert current["canonical_slot"] == "choice.model"
    event = connection.execute(
        "SELECT event_type,origin_class,session_kind,content_json FROM events WHERE event_type='runtime_config_report'"
    ).fetchone()
    assert event is not None
    assert tuple(event[key] for key in ("event_type", "origin_class", "session_kind")) == (
        "runtime_config_report",
        "agent",
        "unknown",
    )
    assert "api_key" not in event["content_json"].casefold()
    database.close()


def test_runtime_report_is_idempotent_while_same_projection_is_active(tmp_path) -> None:
    settings = _settings(tmp_path / "idempotent.db")
    database = Database(settings=settings)
    connection = database.open()

    first = report_extraction_runtime(connection, settings)
    second = report_extraction_runtime(connection, settings)

    assert first.stored is True
    assert second.stored is False
    assert second.reason == "unchanged"
    assert second.claim_id == first.claim_id
    assert connection.execute("SELECT count(*) FROM events WHERE event_type='runtime_config_report'").fetchone()[0] == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM claims WHERE json_extract(qualifiers_json,'$.runtime_config')=1"
        ).fetchone()[0]
        == 1
    )
    database.close()


def test_runtime_report_records_model_change_and_rollback(tmp_path) -> None:
    settings = _settings(tmp_path / "rollback.db")
    database = Database(settings=settings)
    connection = database.open()

    first = report_extraction_runtime(connection, settings)
    second = report_extraction_runtime(connection, replace(settings, llm_model="qwen3.7-plus"))
    third = report_extraction_runtime(connection, settings)

    assert len({first.claim_id, second.claim_id, third.claim_id}) == 3
    rows = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,status,value_json FROM claims WHERE json_extract(qualifiers_json,'$.runtime_config')=1"
        ).fetchall()
    }
    assert rows[first.claim_id]["status"] == "superseded"
    assert rows[second.claim_id]["status"] == "superseded"
    assert rows[third.claim_id]["status"] == "active"
    assert rows[third.claim_id]["value_json"] == '"glm-5.3-flash"'
    assert connection.execute("SELECT count(*) FROM events WHERE event_type='runtime_config_report'").fetchone()[0] == 3
    database.close()


def test_fake_extractor_profile_is_not_reported(tmp_path) -> None:
    settings = replace(Settings.for_test(), database_path=str(tmp_path / "fake.db"), embedding_dim=8)
    database = Database(settings=settings)
    connection = database.open()

    report = report_extraction_runtime(connection, settings)

    assert report.stored is False
    assert report.claim_id is None
    assert report.reason == "fake_profile"
    assert connection.execute("SELECT count(*) FROM events WHERE event_type='runtime_config_report'").fetchone()[0] == 0
    database.close()
