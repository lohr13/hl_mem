import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import hl_mem.workers.worker as worker_module
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.ingest.llm_extractor import PROMPT_HASH, LLMExtractor
from hl_mem.llm.types import LLMResponse
from hl_mem.monitoring.worker import WorkerRuntimeState
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.deferred_tasks import DeferredTaskRepository
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers import job_handlers
from hl_mem.workers import maintenance as maintenance_module
from hl_mem.workers.deferred import process_deferred_tasks
from hl_mem.workers.worker import Worker


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def emit(self, phase, action, outcome, *, detail=None, **_dimensions):
        self.events.append((phase, action, outcome, detail or {}))
        return True

    def cleanup(self, _retention_days, *, batch_size=2_000):
        del batch_size
        return True

    def close(self):
        return True


def test_worker_module_exposes_cli_entrypoint() -> None:
    assert callable(worker_module.main)


def test_worker_reexports_shared_job_dispatch_boundary() -> None:
    assert worker_module.dispatch_job is job_handlers.dispatch_job


def queue(
    connection,
    job_id="job",
    event_id="event",
    max_attempts=3,
    *,
    event_type="message",
    content=None,
    origin_class="unknown",
    session_kind="unknown",
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
            "origin_class": origin_class,
            "session_kind": session_kind,
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


class CountingExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, _content):
        self.calls += 1
        return [ExtractedClaim(predicate="uses", value="SQLite", subject="hl_mem")]


@pytest.mark.parametrize("session_kind", ["heartbeat", "subagent"])
def test_worker_blocks_automated_session_before_extractor_call(tmp_path, session_kind) -> None:
    path = tmp_path / f"{session_kind}.db"
    settings = replace(
        Settings.for_test(),
        database_path=str(path),
        embedding_dim=8,
        provenance_mode="enforce",
    )
    connection = Database(path, settings=settings).open()
    queue(connection, origin_class="system", session_kind=session_kind)
    extractor = CountingExtractor()

    result = Worker(settings, extractor=extractor, embedder=FakeEmbedder(8)).run_once()

    assert result["status"] == "succeeded"
    assert result["claims"] == 0
    assert extractor.calls == 0
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 0


def test_worker_observe_mode_keeps_automated_extraction_flow(tmp_path) -> None:
    path = tmp_path / "observe-heartbeat.db"
    settings = replace(
        Settings.for_test(),
        database_path=str(path),
        embedding_dim=8,
        provenance_mode="observe",
    )
    connection = Database(path, settings=settings).open()
    queue(connection, origin_class="system", session_kind="heartbeat")
    extractor = CountingExtractor()

    result = Worker(settings, extractor=extractor, embedder=FakeEmbedder(8)).run_once()

    assert result["status"] == "succeeded"
    assert extractor.calls == 1
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_worker_rechecks_provenance_for_already_queued_job(tmp_path) -> None:
    path = tmp_path / "queued-regate.db"
    settings = replace(
        Settings.for_test(),
        database_path=str(path),
        embedding_dim=8,
        provenance_mode="enforce",
    )
    connection = Database(path, settings=settings).open()
    queue(connection, origin_class="direct_user", session_kind="interactive")
    connection.execute("UPDATE events SET origin_class='system',session_kind='subagent' WHERE id='event'")
    connection.commit()
    extractor = CountingExtractor()

    result = Worker(settings, extractor=extractor, embedder=FakeEmbedder(8)).run_once()

    assert result["status"] == "succeeded"
    assert extractor.calls == 0


def test_run_once_extracts_and_completes(tmp_path) -> None:
    path = tmp_path / "worker.db"
    connection = Database(path).open()
    queue(connection)
    settings = replace(Settings.for_test(), database_path=str(path), embedding_dim=8)
    result = Worker(settings).run_once()
    assert result["status"] == "succeeded" and result["claims"] == 1
    job = connection.execute(
        "SELECT status,stage,processed,total,progress_detail_json FROM jobs WHERE id='job'"
    ).fetchone()
    assert (job["status"], job["stage"], job["processed"], job["total"]) == (
        "succeeded",
        "claims_written",
        1,
        1,
    )
    assert json.loads(job["progress_detail_json"]) == {
        "written_claim_count": {
            "total": 1,
            "windows": [{"event_ids": ["event"], "written": 1}],
        }
    }
    # Run again to process any relation-discovery job queued after extraction
    Worker(settings).run_once()
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] >= 1
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


class BrokenExtractor:
    def extract(self, _content):
        raise RuntimeError("broken")


class PartiallyInvalidExtractor:
    def extract(self, _content):
        return [
            ExtractedClaim(predicate="uses", value="SQLite", subject="hl_mem"),
            ExtractedClaim(predicate="uses", value="PostgreSQL", subject="hl_mem"),
        ]


def test_failed_extraction_job_keeps_partial_written_claim_count(tmp_path, monkeypatch) -> None:
    path = tmp_path / "partial-write.db"
    connection = Database(path).open()
    queue(connection, max_attempts=1)
    settings = replace(Settings.for_test(), database_path=str(path), embedding_dim=8)
    original_store = worker_module.IngestService.store_extracted
    call_count = 0

    def fail_second_store(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("second claim write failed")
        return original_store(*args, **kwargs)

    monkeypatch.setattr(worker_module.IngestService, "store_extracted", fail_second_store)

    result = Worker(settings, extractor=PartiallyInvalidExtractor(), embedder=FakeEmbedder(8)).run_once()

    assert result["status"] == "dead"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    job = connection.execute(
        "SELECT status,stage,processed,total,progress_detail_json FROM jobs WHERE id='job'"
    ).fetchone()
    assert (job["status"], job["stage"], job["processed"], job["total"]) == (
        "dead",
        "writing_claims",
        1,
        2,
    )
    assert json.loads(job["progress_detail_json"]) == {
        "written_claim_count": {
            "total": 1,
            "windows": [{"event_ids": ["event"], "written": 1}],
        }
    }


class HTTPErrorExtractor:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def extract(self, _content):
        request = httpx.Request("POST", "https://provider.invalid/extract")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f"provider returned {self.status_code}",
            request=request,
            response=response,
        )


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
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
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
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
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
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
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
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
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
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
        extractor=BrokenExtractor(),
    )
    assert worker.run_once()["status"] == "pending"
    assert worker.run_once()["status"] == "dead"


def test_dead_429_extraction_is_deferred_but_other_http_errors_are_not(tmp_path) -> None:
    path = tmp_path / "deferred-429.db"
    connection = Database(path).open()
    queue(connection, job_id="rate-limited", event_id="event-429", max_attempts=1)
    rate_limited = Worker(
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
        extractor=HTTPErrorExtractor(429),
        connection=connection,
    )

    result = rate_limited.run_once()

    task = connection.execute(
        "SELECT task_type,resource_type,resource_id,status,attempts,max_attempts,run_after "
        "FROM deferred_tasks WHERE idempotency_key='retry_extract_event:event-429'"
    ).fetchone()
    assert result["status"] == "dead"
    assert dict(task) == {
        "task_type": "retry_extract_event",
        "resource_type": "event",
        "resource_id": "event-429",
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "run_after": task["run_after"],
    }
    assert datetime.fromisoformat(task["run_after"]) > datetime.now(timezone.utc)

    queue(connection, job_id="server-error", event_id="event-500", max_attempts=1)
    server_error = Worker(
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
        extractor=HTTPErrorExtractor(500),
        connection=connection,
    )
    assert server_error.run_once()["status"] == "dead"
    assert connection.execute("SELECT 1 FROM deferred_tasks WHERE resource_id='event-500'").fetchone() is None


def test_maintenance_requeues_due_deferred_extraction_and_success_closes_it(tmp_path) -> None:
    path = tmp_path / "deferred-maintenance.db"
    connection = Database(path).open()
    queue(connection, job_id="original", event_id="deferred-event", max_attempts=1)
    connection.execute("UPDATE jobs SET status='dead' WHERE id='original'")
    connection.commit()
    repository = DeferredTaskRepository(connection)
    now = datetime.now(timezone.utc)
    repository.defer(
        task_type="retry_extract_event",
        resource_type="event",
        resource_id="deferred-event",
        payload={"event_id": "deferred-event"},
        idempotency_key="retry_extract_event:deferred-event",
        run_after=(now - timedelta(seconds=1)).isoformat(),
        max_attempts=3,
        error="HTTP 429",
        updated_at=now.isoformat(),
    )

    result = process_deferred_tasks(connection, now=now.isoformat())

    task = repository.get_by_idempotency_key("retry_extract_event:deferred-event")
    retry_job = connection.execute(
        "SELECT status,payload_json FROM jobs WHERE idempotency_key=?",
        (f"deferred:{task['id']}:1",),
    ).fetchone()
    assert result == {"registered": 0, "scheduled": 1, "abandoned": 0, "postponed": 0}
    assert task["status"] == "pending" and task["attempts"] == 1
    assert retry_job["status"] == "pending"
    assert json.loads(retry_job["payload_json"])["event_id"] == "deferred-event"

    worker = Worker(
        replace(Settings.for_test(), database_path=str(path), embedding_dim=8),
        connection=connection,
    )
    assert worker.run_once()["status"] == "succeeded"
    assert repository.get_by_idempotency_key("retry_extract_event:deferred-event")["status"] == "completed"


def test_deferred_extraction_stops_after_three_replays(tmp_path) -> None:
    path = tmp_path / "deferred-bounded.db"
    connection = Database(path).open()
    queue(connection, job_id="original", event_id="bounded-event", max_attempts=1)
    connection.execute("UPDATE jobs SET status='dead' WHERE id='original'")
    connection.commit()
    repository = DeferredTaskRepository(connection)
    now = datetime.now(timezone.utc).isoformat()
    repository.defer(
        task_type="retry_extract_event",
        resource_type="event",
        resource_id="bounded-event",
        payload={"event_id": "bounded-event"},
        idempotency_key="retry_extract_event:bounded-event",
        run_after=now,
        max_attempts=3,
        error="HTTP 429",
        updated_at=now,
    )
    connection.execute(
        "UPDATE deferred_tasks SET attempts=max_attempts WHERE idempotency_key=?",
        ("retry_extract_event:bounded-event",),
    )
    connection.commit()

    result = process_deferred_tasks(connection, now=now)

    assert result == {"registered": 0, "scheduled": 0, "abandoned": 1, "postponed": 0}
    assert repository.get_by_idempotency_key("retry_extract_event:bounded-event")["status"] == "abandoned"


def test_maintenance_recovers_only_legacy_dead_429_extractions(tmp_path) -> None:
    path = tmp_path / "legacy-deferred-429.db"
    connection = Database(path).open()
    queue(connection, job_id="legacy-429", event_id="legacy-event-429", max_attempts=1)
    queue(connection, job_id="legacy-budget", event_id="legacy-event-budget", max_attempts=1)
    legacy_failed_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    connection.execute(
        "UPDATE jobs SET status='dead',attempts=1,last_error=?,updated_at=? WHERE id='legacy-429'",
        (
            "Client error '429 Too Many Requests' for url 'https://provider.invalid/chat/completions'",
            legacy_failed_at,
        ),
    )
    connection.execute(
        "UPDATE jobs SET status='dead',attempts=1,last_error='daily token budget exhausted' " "WHERE id='legacy-budget'"
    )
    connection.commit()
    now = datetime.now(timezone.utc).isoformat()

    result = process_deferred_tasks(connection, now=now)

    assert result == {"registered": 1, "scheduled": 1, "abandoned": 0, "postponed": 0}
    assert (
        connection.execute(
            "SELECT status,attempts FROM deferred_tasks WHERE idempotency_key=?",
            ("retry_extract_event:legacy-event-429",),
        ).fetchone()["attempts"]
        == 1
    )
    assert connection.execute("SELECT 1 FROM deferred_tasks WHERE resource_id='legacy-event-budget'").fetchone() is None


def test_lease_prevents_second_worker_from_taking_running_job(tmp_path) -> None:
    path = tmp_path / "lease.db"
    first_db, second_db = Database(path), Database(path)
    queue(first_db.open())
    now = datetime.now(timezone.utc).isoformat()
    assert JobRepository(first_db.open()).lease_job("2999-01-01T00:00:00+00:00", now)
    assert JobRepository(second_db.open()).lease_job("2999-01-01T00:00:00+00:00", now) is None


def test_lost_lease_never_reports_success(tmp_path, monkeypatch) -> None:
    path = tmp_path / "lost-lease.db"
    connection = Database(path).open()
    queue(connection)
    worker = Worker(replace(Settings.for_test(), database_path=str(path), embedding_dim=8))
    monkeypatch.setattr(worker.jobs, "complete_jobs", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(worker.jobs, "fail_jobs", lambda *_args, **_kwargs: 0)

    result = worker.run_once()

    assert result["status"] == "lease_lost"
    assert "lease ownership lost" in result["error"]
    worker.close()


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

    monkeypatch.setattr(maintenance_module, "cleanup_stale_temporal_claims", broken_cleanup)
    monkeypatch.setattr(maintenance_module, "expire_claims", healthy_expiration)

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


def test_maintenance_reviews_pending_near_duplicates_without_llm(monkeypatch, tmp_path) -> None:
    worker = Worker(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "maintenance-dedup.db"),
            dedup_enabled=True,
            dedup_threshold=0.93,
            dedup_scan_limit=17,
        )
    )
    calls: list[tuple[float, int]] = []

    def review(_connection, *, threshold, limit):
        calls.append((threshold, limit))
        return {"scanned": 0, "equivalent": 0, "deferred": 0, "missing": 0}

    monkeypatch.setattr(maintenance_module, "review_pending_near_duplicates", review)

    worker._run_maintenance()

    assert calls == [(0.93, 17)]
    worker.database.close()


def test_maintenance_observes_expired_cleanup_with_bounded_settings(monkeypatch, tmp_path) -> None:
    worker = Worker(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "maintenance-expired.db"),
            expired_cleanup_mode="observe",
            expired_claim_retention_days=120,
            expired_cleanup_batch_size=7,
        )
    )
    calls: list[tuple[int, int, str]] = []

    def maintain(_connection, *, now, retention_days, batch_size, mode):
        assert now
        calls.append((retention_days, batch_size, mode))
        return {"eligible_claim_count": 0, "deleted": 0}

    monkeypatch.setattr(maintenance_module, "maintain_expired_claims", maintain)

    worker._run_maintenance()

    assert calls == [(120, 7, "observe")]
    worker.database.close()


def test_maintenance_passes_conflict_budget_and_records_result(monkeypatch, tmp_path) -> None:
    runtime = WorkerRuntimeState()
    worker = Worker(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "maintenance-conflicts.db"),
            conflict_maintenance_max_cases=7,
            conflict_maintenance_budget_ms=321,
            conflict_failure_backoff_seconds=45,
            conflict_writer_yield_ms=0,
            conflict_auto_mode="l0_only",
        ),
        worker_runtime=runtime,
    )
    calls: list[tuple[int, int, int]] = []

    def resolve(
        _connection,
        _now,
        *,
        max_cases,
        max_elapsed_ms,
        failure_backoff_seconds,
    ):
        calls.append(
            (
                max_cases,
                max_elapsed_ms,
                failure_backoff_seconds,
            )
        )
        return {"scanned": 2, "changed": 1, "dirty_ready": 4, "dirty_blocked": 1}

    monkeypatch.setattr(maintenance_module, "auto_resolve_conflicts", resolve)

    worker._run_maintenance()

    assert calls == [(7, 321, 45)]
    assert runtime.snapshot()["last_maintenance_results"]["auto_resolve_conflicts"] == {
        "scanned": 2,
        "changed": 1,
        "dirty_ready": 4,
        "dirty_blocked": 1,
    }
    assert runtime.snapshot()["current_maintenance_item"] is None
    worker.database.close()


def test_maintenance_conflict_kill_switch_skips_auto_resolve(monkeypatch, tmp_path) -> None:
    worker = Worker(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "maintenance-conflicts-disabled.db"),
            conflict_auto_resolve_enabled=False,
        )
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("auto resolve must be disabled")

    monkeypatch.setattr(maintenance_module, "auto_resolve_conflicts", unexpected)

    worker._run_maintenance()

    worker.database.close()
