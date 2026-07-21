import json
from datetime import datetime, timezone

import hl_mem.workers.worker as worker_module
from hl_mem.storage.database import Database
from hl_mem.storage.repository import EventRepository, JobRepository
from hl_mem.workers.worker import Worker


def test_worker_module_exposes_cli_entrypoint() -> None:
    assert callable(worker_module.main)


def queue(connection, job_id="job", event_id="event", max_attempts=3) -> None:
    now = datetime.now(timezone.utc).isoformat()
    EventRepository(connection).insert_event({
        "id": event_id, "event_type": "message", "actor_type": "user",
        "content_json": '{"text":"记住使用 SQLite"}', "occurred_at": now, "recorded_at": now,
    })
    JobRepository(connection).insert_job({
        "id": job_id, "job_type": "extract_event", "payload_json": json.dumps({"event_id": event_id}),
        "created_at": now, "updated_at": now, "max_attempts": max_attempts,
    })


def test_run_once_extracts_and_completes(tmp_path) -> None:
    path = tmp_path / "worker.db"
    connection = Database(path).open()
    queue(connection)
    result = Worker(path, {"embedding_dim": 8}).run_once()
    assert result["status"] == "succeeded" and result["claims"] == 1
    assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "succeeded"
    assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


class BrokenExtractor:
    def extract(self, _content):
        raise RuntimeError("broken")


def test_failure_retries_then_becomes_dead(tmp_path) -> None:
    path = tmp_path / "failure.db"
    connection = Database(path).open()
    queue(connection, max_attempts=2)
    worker = Worker(path, {"extractor": BrokenExtractor(), "embedding_dim": 8})
    assert worker.run_once()["status"] == "pending"
    assert worker.run_once()["status"] == "dead"


def test_lease_prevents_second_worker_from_taking_running_job(tmp_path) -> None:
    path = tmp_path / "lease.db"
    first_db, second_db = Database(path), Database(path)
    queue(first_db.open())
    now = datetime.now(timezone.utc).isoformat()
    assert JobRepository(first_db.open()).lease_job("2999-01-01T00:00:00+00:00", now)
    assert JobRepository(second_db.open()).lease_job("2999-01-01T00:00:00+00:00", now) is None
