"""并发写入竞态测试。"""

import threading
from typing import Any

from hl_mem.application.ingest import IngestService

def store_extracted(conn, claim, event, now, embedder, **kw):
    return IngestService.store_extracted(conn, claim, event, now, embedder, **kw)
from hl_mem.application.ingest import IngestService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.database import Database
from hl_mem.storage.jobs import JobRepository


def test_concurrent_idempotent_event_write(tmp_path: Any) -> None:
    """两个线程写入相同幂等键时只创建一个事件。"""
    database_path = tmp_path / "concurrent.db"
    databases = [Database(database_path), Database(database_path)]
    barrier = threading.Barrier(2)
    results: list[dict[str, Any] | None] = [None, None]

    def write(index: int, database: Database) -> None:
        connection = database.open()
        service = IngestService(connection, FakeEmbedder(2048))
        barrier.wait()
        results[index] = service.ingest_event(
            {"event_type": "message", "actor_type": "user", "content": {"text": "test"}},
            idempotency_key="same-key",
        )

    threads = [
        threading.Thread(target=write, args=(index, database))
        for index, database in enumerate(databases)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results[0] is not None
    assert results[1] is not None
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["created"] is not results[1]["created"]
    connection = databases[0].open()
    count = connection.execute(
        "SELECT count(*) FROM events WHERE idempotency_key=?",
        ("same-key",),
    ).fetchone()[0]
    assert count == 1
    for database in databases:
        database.close()


def test_concurrent_claim_dedup(tmp_path: Any) -> None:
    """两个线程写入相同事实哈希时只创建一个活跃 claim。"""
    database_path = tmp_path / "dedup.db"
    databases = [Database(database_path), Database(database_path)]
    for database in databases:
        database.open_worker()
    barrier = threading.Barrier(2)
    results: list[str | None] = [None, None]

    def store(index: int, database: Database) -> None:
        connection = database.open_worker()
        extracted = ExtractedClaim(
            predicate="likes",
            value="coffee",
            confidence=0.9,
            volatility="stable",
            subject="user",
            qualifiers={},
            scope="permanent",
            importance=0.8,
            canonical_attribute=None,
        )
        event = {
            "id": f"event-{index}",
            "actor_type": "user",
            "occurred_at": "2026-01-01T00:00:00+00:00",
        }
        barrier.wait()
        results[index] = store_extracted(
            connection,
            extracted,
            event,
            "2026-01-01T00:00:00+00:00",
            FakeEmbedder(2048),
        )

    threads = [
        threading.Thread(target=store, args=(index, database))
        for index, database in enumerate(databases)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results[0] == results[1]
    connection = databases[0].open_worker()
    count = connection.execute(
        "SELECT count(*) FROM claims "
        "WHERE subject_entity_id=? AND predicate=? AND status='active'",
        ("user", "likes"),
    ).fetchone()[0]
    assert count == 1
    for database in databases:
        database.close()


def test_lease_token_prevents_old_worker_completion(tmp_path: Any) -> None:
    """lease 过期后拒绝旧 worker 使用原令牌完成任务。"""
    database_path = tmp_path / "lease.db"
    first_database = Database(database_path)
    first_connection = first_database.open_worker()
    first_jobs = JobRepository(first_connection)
    now = "2026-01-01T00:00:00+00:00"

    first_jobs.insert_job(
        {
            "id": "job-1",
            "job_type": "extract_event",
            "payload_json": "{}",
            "idempotency_key": "test-1",
            "created_at": now,
            "updated_at": now,
        }
    )

    first_lease = first_jobs.lease_job("2999-01-01T00:00:00+00:00", now)
    assert first_lease is not None
    first_token = first_lease["lease_token"]

    second_database = Database(database_path)
    second_connection = second_database.open_worker()
    second_jobs = JobRepository(second_connection)
    assert second_jobs.lease_job("2999-01-01T00:00:00+00:00", now) is None

    second_connection.execute(
        "UPDATE jobs SET leased_until='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
    )
    second_connection.commit()

    second_lease = second_jobs.lease_job("2999-01-01T00:00:00+00:00", now)
    assert second_lease is not None
    second_token = second_lease["lease_token"]
    assert second_token != first_token

    assert first_jobs.complete_job("job-1", now, first_token) is False
    assert second_jobs.complete_job("job-1", now, second_token) is True

    first_database.close()
    second_database.close()
