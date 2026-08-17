"""召回副作用的非阻塞投递与 deferred worker 回归。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

from hl_mem.application.recall import RecallService
from hl_mem.application.recall_side_effects import (
    DeferredAuditLogger,
    DeferredLLMSpanRecorder,
    RecallSideEffectDispatcher,
)
from hl_mem.ingest.embedder import FakeEmbedder, pack_vector
from hl_mem.observability.audit import AuditLogger, audit_scope
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.deferred_tasks import DeferredTaskRepository
from hl_mem.workers.deferred import cleanup_recall_side_effect_tasks, process_recall_side_effect_tasks
from hl_mem.workers.worker import _process_recall_side_effects_safely

NOW = "2026-08-18T00:00:00+00:00"


def _seed_claim(connection, claim_id: str = "claim-1") -> None:
    ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "status": "active",
            "subject_entity_id": "user",
            "predicate": "likes",
            "value_json": '"tea"',
            "index_text": "likes tea",
            "qualifiers": {"role": "user", "action": "likes", "object": "tea"},
            "recorded_from": NOW,
        },
        commit=False,
    )
    connection.commit()


def test_dispatch_is_non_blocking_while_another_connection_holds_write_lock(tmp_path) -> None:
    path = tmp_path / "dispatch-lock.db"
    settings = replace(Settings.for_test(), database_path=str(path), database_busy_timeout_seconds=5.0)
    database = Database(settings=settings)
    writer = database.open()
    writer.execute("BEGIN IMMEDIATE")
    dispatcher = RecallSideEffectDispatcher(database, settings=settings)

    started = time.monotonic()
    accepted = dispatcher.submit_access("query-1", ["claim-1"], NOW)
    elapsed = time.monotonic() - started

    assert accepted is True
    assert elapsed < 0.2
    assert writer.execute("SELECT count(*) FROM deferred_tasks").fetchone()[0] == 0

    writer.rollback()
    assert dispatcher.drain(2.0) is True
    assert tuple(writer.execute("SELECT task_type,idempotency_key,status FROM deferred_tasks").fetchone()) == (
        "record_recall_access",
        "record_recall_access:query-1",
        "pending",
    )
    queued_health = dispatcher.health(writer)["access_record"]
    assert queued_health["submitted"] == 1
    assert queued_health["persisted"] == 1
    assert queued_health["completed"] == 0
    dispatcher.close(2.0)
    database.close()


def test_dispatch_retries_durable_enqueue_after_busy_timeout(tmp_path) -> None:
    path = tmp_path / "dispatch-retry.db"
    settings = replace(
        Settings.for_test(),
        database_path=str(path),
        recall_side_effect_max_attempts=4,
        recall_side_effect_backoff_seconds=0.02,
    )
    database = Database(settings=settings, busy_timeout_seconds=0.05)
    writer = database.open()
    writer.execute("BEGIN IMMEDIATE")
    dispatcher = RecallSideEffectDispatcher(database, settings=settings)

    assert dispatcher.submit_access("query-retry", ["claim-1"], NOW) is True
    deadline = time.monotonic() + 1.0
    while int(dispatcher.health()["access_record"]["failures"] or 0) < 1:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    writer.rollback()

    assert dispatcher.drain(2.0) is True
    assert (
        writer.execute(
            "SELECT count(*) FROM deferred_tasks WHERE idempotency_key='record_recall_access:query-retry'"
        ).fetchone()[0]
        == 1
    )
    health = dispatcher.health(writer)["access_record"]
    assert health["persisted"] == 1
    assert int(health["failures"] or 0) >= 1
    dispatcher.close(2.0)
    database.close()


def test_dispatch_tracks_exposure_until_durable_enqueue(tmp_path) -> None:
    database = Database(tmp_path / "dispatch-exposure-window.db", busy_timeout_seconds=0.05)
    writer = database.open()
    writer.execute("BEGIN IMMEDIATE")
    dispatcher = RecallSideEffectDispatcher(database, settings=Settings.for_test())
    exposure = ("feedback-pending", "query-1", "claim", "claim-1", 1, 0.9, NOW)

    assert dispatcher.submit_exposures("query-1", [exposure]) is True
    assert dispatcher.has_pending_exposures(["feedback-pending"]) is True

    writer.rollback()
    assert dispatcher.drain(2.0) is True
    assert dispatcher.has_pending_exposures(["feedback-pending"]) is False
    assert DeferredTaskRepository(writer).has_pending_recall_exposure("feedback-pending") is True
    dispatcher.close(2.0)
    database.close()


def test_worker_applies_access_and_exposure_once_when_tasks_are_replayed(tmp_path) -> None:
    database = Database(tmp_path / "worker-idempotency.db")
    connection = database.open()
    _seed_claim(connection)
    repository = DeferredTaskRepository(connection)
    repository.defer(
        task_type="record_recall_access",
        resource_type="query",
        resource_id="query-1",
        payload={"claim_ids": ["claim-1"], "accessed_at": NOW},
        idempotency_key="record_recall_access:query-1",
        run_after=NOW,
        max_attempts=3,
        error="",
        updated_at=NOW,
    )
    repository.defer(
        task_type="record_recall_exposures",
        resource_type="query",
        resource_id="query-1",
        payload={
            "exposures": [["feedback-1", "query-1", "claim", "claim-1", 1, 0.9, NOW]],
        },
        idempotency_key="record_recall_exposures:feedback-1",
        run_after=NOW,
        max_attempts=3,
        error="",
        updated_at=NOW,
    )

    first = process_recall_side_effect_tasks(connection, now=NOW)
    second = process_recall_side_effect_tasks(connection, now=NOW)

    assert first["completed"] == 2
    assert second["completed"] == 0
    assert tuple(
        connection.execute("SELECT access_count,last_accessed_at FROM claims WHERE id='claim-1'").fetchone()
    ) == (1, NOW)
    assert tuple(connection.execute("SELECT id,query_id,memory_id,injected FROM retrieval_feedback").fetchone()) == (
        "feedback-1",
        "query-1",
        "claim-1",
        0,
    )
    assert {row[0] for row in connection.execute("SELECT status FROM deferred_tasks").fetchall()} == {"completed"}
    database.close()


def test_record_access_can_join_worker_transaction(tmp_path) -> None:
    database = Database(tmp_path / "access-transaction.db")
    connection = database.open()
    _seed_claim(connection)

    connection.execute("BEGIN IMMEDIATE")
    ClaimRepository(connection).record_access(["claim-1"], NOW, commit=False)
    assert connection.in_transaction is True
    connection.rollback()

    assert connection.execute("SELECT access_count FROM claims WHERE id='claim-1'").fetchone()[0] == 0
    database.close()


def test_worker_records_failed_attempt_then_retries_exposure(tmp_path) -> None:
    database = Database(tmp_path / "worker-retry.db")
    connection = database.open()
    _seed_claim(connection)
    repository = DeferredTaskRepository(connection)
    repository.defer(
        task_type="record_recall_exposures",
        resource_type="query",
        resource_id="query-retry",
        payload={
            "exposures": [["feedback-retry", "query-retry", "claim", "claim-1", 1, None, NOW]],
        },
        idempotency_key="record_recall_exposures:feedback-retry",
        run_after=NOW,
        max_attempts=3,
        error="",
        updated_at=NOW,
    )
    connection.execute(
        "CREATE TRIGGER fail_recall_exposure BEFORE INSERT ON retrieval_feedback "
        "BEGIN SELECT RAISE(ABORT,'blocked exposure'); END"
    )
    connection.commit()

    failed = process_recall_side_effect_tasks(connection, now=NOW)
    task = repository.get_by_idempotency_key("record_recall_exposures:feedback-retry")
    assert failed["retried"] == 1
    assert task["status"] == "pending"
    assert task["attempts"] == 1
    assert "blocked exposure" in task["last_error"]

    connection.execute("DROP TRIGGER fail_recall_exposure")
    connection.execute("UPDATE deferred_tasks SET run_after=? WHERE id=?", (NOW, task["id"]))
    connection.commit()
    recovered = process_recall_side_effect_tasks(connection, now=NOW)
    assert recovered["completed"] == 1
    assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 1
    database.close()


def test_recall_on_readonly_connection_submits_side_effects_without_sql_writes(tmp_path) -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.access: list[tuple[str, list[str], str]] = []
            self.exposures: list[tuple[str, list[tuple[object, ...]]]] = []

        def submit_access(self, query_id: str, claim_ids: list[str], accessed_at: str) -> bool:
            self.access.append((query_id, claim_ids, accessed_at))
            return True

        def submit_exposures(self, query_id: str, exposures: list[tuple[object, ...]]) -> bool:
            self.exposures.append((query_id, exposures))
            return True

    database = Database(tmp_path / "recall-zero-write.db")
    writer = database.open()
    _seed_claim(writer)
    sink = RecordingSink()
    settings = replace(
        Settings.for_test(),
        embedding_dim=4,
        recall_dense_enabled=False,
        resurrection_mode="off",
    )
    writes: list[int] = []
    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_TRANSACTION,
    }

    with database.connect_readonly() as reader:
        reader.set_authorizer(
            lambda action, _arg1, _arg2, _db, _trigger: (
                writes.append(action) or sqlite3.SQLITE_OK if action in write_actions else sqlite3.SQLITE_OK
            )
        )
        response = RecallService(
            reader,
            FakeEmbedder(4),
            settings=settings,
            side_effect_sink=sink,
        ).recall(
            "likes tea",
            limit=1,
            query_id="query-zero-write",
            response_format="both",
        )

    assert writes == []
    assert sink.access[0][0:2] == ("query-zero-write", ["claim-1"])
    assert sink.exposures[0][0] == "query-zero-write"
    assert response["context_packet"]["feedback_state"] == "available"
    assert response["results"][0]["id"] == "claim-1"
    database.close()


def test_recall_audit_and_llm_span_are_dispatched_outside_the_request_write_lock(tmp_path) -> None:
    path = tmp_path / "deferred-observability.db"
    settings = replace(Settings.for_test(), database_path=str(path), database_busy_timeout_seconds=5.0)
    database = Database(settings=settings)
    writer = database.open()
    audit = AuditLogger(path, busy_timeout_ms=5000)
    dispatcher = RecallSideEffectDispatcher(database, settings=settings)
    deferred_audit = DeferredAuditLogger(audit, dispatcher)
    deferred_spans = DeferredLLMSpanRecorder(dispatcher)
    writer.execute("BEGIN IMMEDIATE")

    started = time.monotonic()
    with audit_scope(deferred_audit, trace_id="trace-1", query_id="query-1"):
        assert deferred_audit.emit("recall", "ranked", "success", detail={"limit": 1}) is True
    assert (
        deferred_spans.record(
            operation="query_expansion",
            provider="dashscope",
            model="model",
            structured_mode="json_object",
            status="success",
            latency_ms=10.0,
            started_at=NOW,
        )
        is None
    )
    assert time.monotonic() - started < 0.2
    assert writer.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0
    assert writer.execute("SELECT count(*) FROM llm_call_spans").fetchone()[0] == 0

    writer.rollback()
    assert dispatcher.drain(2.0) is True
    assert tuple(writer.execute("SELECT phase,action,outcome,trace_id,query_id FROM audit_log").fetchone()) == (
        "recall",
        "ranked",
        "success",
        "trace-1",
        "query-1",
    )
    assert tuple(writer.execute("SELECT operation,status,provider,model FROM llm_call_spans").fetchone()) == (
        "query_expansion",
        "success",
        "dashscope",
        "model",
    )
    dispatcher.close(2.0)
    audit.close()
    database.close()


def test_rejected_audit_is_not_counted_as_persisted(tmp_path) -> None:
    class RejectingAudit:
        enabled = True

        @staticmethod
        def emit(*_args, **_kwargs) -> bool:
            return False

    database = Database(tmp_path / "rejected-audit.db")
    database.open()
    dispatcher = RecallSideEffectDispatcher(database)

    assert dispatcher.submit_audit(RejectingAudit(), ("recall", "ranked", "success"), {}) is True
    assert dispatcher.drain(2.0) is True

    health = dispatcher.health()["audit_emit"]
    assert health["persisted"] == 0
    assert health["failures"] == 1
    dispatcher.close(2.0)
    database.close()


def test_cleanup_recall_side_effect_tasks_removes_only_old_terminal_rows(tmp_path) -> None:
    database = Database(tmp_path / "cleanup-recall-tasks.db")
    connection = database.open()
    repository = DeferredTaskRepository(connection)
    for key, task_type in (
        ("old-completed", "record_recall_access"),
        ("old-pending", "record_recall_exposures"),
        ("new-completed", "record_recall_access"),
        ("legacy-completed", "retry_extract_event"),
    ):
        repository.defer(
            task_type=task_type,
            resource_type="query",
            resource_id=key,
            payload={},
            idempotency_key=key,
            run_after=NOW,
            max_attempts=3,
            error="",
            updated_at=NOW,
        )
    connection.execute(
        "UPDATE deferred_tasks SET status='completed',updated_at='2026-08-01T00:00:00+00:00' "
        "WHERE idempotency_key IN ('old-completed','legacy-completed')"
    )
    connection.execute(
        "UPDATE deferred_tasks SET status='completed',updated_at='2026-08-18T00:00:00+00:00' "
        "WHERE idempotency_key='new-completed'"
    )
    connection.commit()

    removed = cleanup_recall_side_effect_tasks(connection, before="2026-08-10T00:00:00+00:00")

    assert removed == 1
    assert {row[0] for row in connection.execute("SELECT idempotency_key FROM deferred_tasks")} == {
        "old-pending",
        "new-completed",
        "legacy-completed",
    }
    database.close()


def test_worker_loop_isolates_recall_side_effect_consumer_failure(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "worker-isolation.db")
    connection = database.open()
    connection.execute("BEGIN")
    monkeypatch.setattr(
        "hl_mem.workers.worker.process_recall_side_effect_tasks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )

    result = _process_recall_side_effects_safely(connection, NOW)

    assert result == {"completed": 0, "retried": 0, "abandoned": 0}
    assert connection.in_transaction is False
    database.close()


def test_worker_revalidates_and_applies_deferred_resurrection(tmp_path) -> None:
    path = tmp_path / "worker-resurrection.db"
    settings = replace(Settings.for_test(), database_path=str(path), embedding_dim=2)
    database = Database(settings=settings)
    connection = database.open()
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES ('event-1','message','user','{}',?,?)",
        (NOW, NOW),
    )
    ClaimRepository(connection).insert_claim(
        {
            "id": "archived-1",
            "status": "archived",
            "namespace_key": "default",
            "subject_entity_id": "user",
            "predicate": "likes",
            "value_json": '"tea"',
            "index_text": "likes tea",
            "recorded_from": NOW,
        },
        commit=False,
    )
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('link-1','claim','archived-1','event','event-1','derived_from')"
    )
    connection.commit()
    dispatcher = RecallSideEffectDispatcher(database, settings=settings)
    assert dispatcher.submit_resurrection(
        "query-1",
        "archived-1",
        pack_vector([1.0, 0.0]),
        "resurrection-test",
        2,
        namespace="default",
        as_of=NOW,
        known_as_of=None,
    )
    assert dispatcher.drain(2.0)

    result = process_recall_side_effect_tasks(connection, now=NOW)

    assert result["completed"] == 1
    row = connection.execute(
        "SELECT status,embedding_dense,embedding_model,embedding_dim FROM claims WHERE id='archived-1'"
    ).fetchone()
    assert tuple(row) == ("active", pack_vector([1.0, 0.0]), "resurrection-test", 2)
    dispatcher.close(2.0)
    database.close()
