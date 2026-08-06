import json
import logging
from dataclasses import replace
from datetime import datetime, timezone

import hl_mem.workers.worker as worker_module
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.llm_extractor import PROMPT_HASH, LLMExtractor
from hl_mem.llm.types import LLMResponse
from hl_mem.monitoring.worker import WorkerRuntimeState
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.worker import Worker


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def emit(self, phase, action, outcome, *, detail=None, **_dimensions):
        self.events.append((phase, action, outcome, detail or {}))
        return True

    def cleanup(self, _retention_days):
        return True

    def close(self):
        return True


def test_worker_module_exposes_cli_entrypoint() -> None:
    assert callable(worker_module.main)


def queue(
    connection,
    job_id="job",
    event_id="event",
    max_attempts=3,
    *,
    event_type="message",
    content=None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    EventRepository(connection).insert_event(
        {
            "id": event_id,
            "event_type": event_type,
            "actor_type": "user",
            "content_json": json.dumps(content or {"text": "记住使用 SQLite"}, ensure_ascii=False),
            "occurred_at": now,
            "recorded_at": now,
        }
    )
    JobRepository(connection).insert_job(
        {
            "id": job_id,
            "job_type": "extract_event",
            "payload_json": json.dumps({"event_id": event_id}),
            "created_at": now,
            "updated_at": now,
            "max_attempts": max_attempts,
        }
    )


def test_run_once_extracts_and_completes(tmp_path) -> None:
    path = tmp_path / "worker.db"
    connection = Database(path).open()
    queue(connection)
    settings = Settings(database_path=str(path), embedding_dim=8)
    result = Worker(settings).run_once()
    assert result["status"] == "succeeded" and result["claims"] == 1
    # Run again to process any relation-discovery job queued after extraction
    Worker(settings).run_once()
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 1
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


class BrokenExtractor:
    def extract(self, _content):
        raise RuntimeError("broken")


class NoClaimsLLMClient:
    class Provider:
        name = "fake"

    provider = Provider()
    model = "test-model"

    def complete(self, _request):
        return LLMResponse('{"claims":[],"should_memorize":false}', "stop", 4)


class OneClaimLLMClient(NoClaimsLLMClient):
    def complete(self, _request):
        return LLMResponse(
            '{"claims":[{"subject":"hl_mem","value":"hl_mem 使用 SQLite",'
            '"kind":"choice","confidence":0.9,"notability":"high",'
            '"evidence_quote":"使用 SQLite"}],"should_memorize":true}',
            "stop",
            4,
        )


class CustomVersionLLMExtractor(LLMExtractor):
    prompt_hash = "111111111111"
    extractor_version = "llm-v2+111111111111"


def test_llm_extraction_audit_records_prompt_hash(tmp_path) -> None:
    path = tmp_path / "prompt-hash-audit.db"
    connection = Database(path).open()
    queue(connection)
    audit = RecordingAudit()
    extractor = LLMExtractor(NoClaimsLLMClient(), ChunkingPolicy(10_000, 0, 2))
    worker = Worker(
        Settings(database_path=str(path), embedding_dim=8),
        extractor=extractor,
        embedder=FakeEmbedder(8),
        image_describer=None,
        audit_logger=audit,
    )

    result = worker.run_once()

    extraction_events = [event for event in audit.events if event[:2] == ("extraction", "evaluated")]
    assert result["status"] == "succeeded"
    assert extraction_events[0][3]["extractor_hash"] == PROMPT_HASH


def test_worker_carries_actual_llm_extractor_version_to_claim(tmp_path) -> None:
    path = tmp_path / "actual-extractor-version.db"
    connection = Database(path).open()
    queue(connection)
    extractor = CustomVersionLLMExtractor(OneClaimLLMClient(), ChunkingPolicy(10_000, 0, 2))
    worker = Worker(
        Settings(database_path=str(path), embedding_dim=8),
        extractor=extractor,
        embedder=FakeEmbedder(8),
        image_describer=None,
    )

    assert worker.run_once()["status"] == "succeeded"
    row = connection.execute("SELECT extractor_version FROM claims").fetchone()
    assert row["extractor_version"] == extractor.extractor_version


def test_explicit_memory_does_not_claim_llm_prompt_provenance(tmp_path) -> None:
    path = tmp_path / "explicit-extractor-version.db"
    connection = Database(path).open()
    queue(
        connection,
        event_type="explicit_memory",
        content={
            "text": "用户要求显式记住 SQLite",
            "memory": {
                "predicate": "explicit_memory",
                "text": "用户要求显式记住 SQLite",
                "subject": "用户",
                "qualifiers": {},
            },
        },
    )
    audit = RecordingAudit()
    extractor = LLMExtractor(NoClaimsLLMClient(), ChunkingPolicy(10_000, 0, 2))
    worker = Worker(
        Settings(database_path=str(path), embedding_dim=8),
        extractor=extractor,
        embedder=FakeEmbedder(8),
        image_describer=None,
        audit_logger=audit,
    )

    assert worker.run_once()["status"] == "succeeded"
    row = connection.execute("SELECT extractor_version FROM claims").fetchone()
    extraction_event = next(event for event in audit.events if event[:2] == ("extraction", "evaluated"))
    assert row["extractor_version"] == "explicit-v1"
    assert "extractor_hash" not in extraction_event[3]


def test_explicit_memory_without_bypass_payload_records_actual_llm_provenance(tmp_path) -> None:
    path = tmp_path / "explicit-llm-fallback-version.db"
    connection = Database(path).open()
    queue(
        connection,
        event_type="explicit_memory",
        content={"text": "hl_mem 使用 SQLite"},
    )
    audit = RecordingAudit()
    extractor = CustomVersionLLMExtractor(OneClaimLLMClient(), ChunkingPolicy(10_000, 0, 2))
    worker = Worker(
        Settings(database_path=str(path), embedding_dim=8),
        extractor=extractor,
        embedder=FakeEmbedder(8),
        image_describer=None,
        audit_logger=audit,
    )

    assert worker.run_once()["status"] == "succeeded"
    row = connection.execute("SELECT extractor_version FROM claims").fetchone()
    extraction_event = next(event for event in audit.events if event[:2] == ("extraction", "evaluated"))
    assert row["extractor_version"] == extractor.extractor_version
    assert extraction_event[3]["extractor_hash"] == extractor.prompt_hash


def test_failure_retries_then_becomes_dead(tmp_path) -> None:
    path = tmp_path / "failure.db"
    connection = Database(path).open()
    queue(connection, max_attempts=2)
    worker = Worker(
        Settings(database_path=str(path), embedding_dim=8),
        extractor=BrokenExtractor(),
    )
    assert worker.run_once()["status"] == "pending"
    assert worker.run_once()["status"] == "dead"


def test_lease_prevents_second_worker_from_taking_running_job(tmp_path) -> None:
    path = tmp_path / "lease.db"
    first_db, second_db = Database(path), Database(path)
    queue(first_db.open())
    now = datetime.now(timezone.utc).isoformat()
    assert JobRepository(first_db.open()).lease_job("2999-01-01T00:00:00+00:00", now)
    assert JobRepository(second_db.open()).lease_job("2999-01-01T00:00:00+00:00", now) is None


def test_maintenance_failure_rolls_back_and_does_not_stop_later_items(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    runtime = WorkerRuntimeState()
    audit = RecordingAudit()
    worker = Worker(
        replace(Settings.for_test(), database_path=str(tmp_path / "maintenance.db")),
        audit_logger=audit,
        worker_runtime=runtime,
    )
    later_items: list[str] = []

    def broken_cleanup(connection, **_kwargs):
        connection.execute("BEGIN IMMEDIATE")
        raise RuntimeError("cleanup exploded")

    def healthy_expiration(connection, **_kwargs):
        assert connection.in_transaction is False
        later_items.append("expire_claims")
        return {"expired": 0}

    monkeypatch.setattr(worker_module, "cleanup_stale_temporal_claims", broken_cleanup)
    monkeypatch.setattr(worker_module, "expire_claims", healthy_expiration)

    with caplog.at_level(logging.ERROR, logger="hl_mem.workers.worker"):
        worker._run_maintenance()

    snapshot = runtime.snapshot()
    assert later_items == ["expire_claims"]
    assert snapshot["maintenance_runs"] == 1
    assert snapshot["maintenance_failures"] == 1
    assert snapshot["failure_counts"] == {"cleanup_stale_temporal_claims": 1}
    assert snapshot["last_maintenance_completed_at"] is not None
    assert audit.events == [
        (
            "worker",
            "maintenance",
            "error",
            {
                "item": "cleanup_stale_temporal_claims",
                "error_class": "RuntimeError",
                "error": "cleanup exploded",
            },
        )
    ]
    assert "worker_maintenance_failed item=cleanup_stale_temporal_claims" in caplog.text
    worker.database.close()
