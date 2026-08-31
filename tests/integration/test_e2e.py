from dataclasses import replace

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.worker import Worker


def event(key: str, session: str, text: str) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "session_id": session,
        "event_type": "message",
        "actor_type": "user",
        "content": {"text": text},
    }


def test_idempotency_cross_session_and_evidence(tmp_path) -> None:
    app = create_app(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "e2e.db"),
            relation_discovery_mode="audit",
            llm_api_key="test-key",
        )
    )
    with TestClient(app) as client:
        first = client.post("/v1/events", json=event("key-1", "s1", "我喜欢 PostgreSQL"))
        duplicate = client.post("/v1/events", json=event("key-1", "s1", "我喜欢 PostgreSQL"))
        client.post("/v1/events", json=event("key-2", "s2", "记住 PostgreSQL 开启备份"))
        worker = Worker(app.state.settings, relation_discoverer=object())
        assert worker.run_once()["status"] == "succeeded"
        assert worker.run_once()["status"] == "succeeded"
        response = client.post("/v1/recall", json={"query": "PostgreSQL", "session_id": "s3"})
        assert first.json()["created"] is True
        assert duplicate.json() == {"id": first.json()["id"], "created": False}
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert all(item["evidence"] and item["evidence"][0]["type"] == "event" for item in response.json()["results"])
        connection = app.state.db.open()
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4  # 2 extract + 2 relation-discovery


def test_data_survives_database_restart(tmp_path) -> None:
    path = tmp_path / "restart.db"
    with TestClient(create_app(path)) as client:
        client.post("/v1/events", json=event("persist-1", "s1", "记住使用 SQLite 持久化"))
    assert Worker(replace(Settings.for_test(), database_path=str(path))).run_once()["status"] == "succeeded"
    with TestClient(create_app(path)) as client:
        response = client.post("/v1/recall", json={"query": "SQLite", "session_id": "s2"})
        assert response.json()["total"] == 1
        assert response.json()["results"][0]["evidence"]


def test_healthz_does_not_open_a_database_connection(monkeypatch, tmp_path) -> None:
    from hl_mem import __version__

    app = create_app(tmp_path / "health.db")
    with TestClient(app) as client:

        def fail_connect() -> None:
            raise AssertionError("healthz must not open a database connection")

        monkeypatch.setattr(app.state.db, "connect", fail_connect)
        result = client.get("/healthz").json()
        assert result["status"] == "ok"
        assert result["version"] == __version__
        assert "embedder" in result
        assert "reranker" in result


def test_jobs_api_includes_progress_fields(tmp_path) -> None:
    """任务列表 API 应保留状态计数并返回进度明细。"""
    path = tmp_path / "jobs-api.db"
    database = Database(path)
    connection = database.open()
    JobRepository(connection).insert_job(
        {
            "id": "job-api",
            "job_type": "deduplicate_claims",
            "payload_json": "{}",
            "stage": "queued",
            "processed": 0,
            "total": 5,
            "created_at": "2026-07-24T00:00:00+00:00",
            "updated_at": "2026-07-24T00:00:00+00:00",
        }
    )
    database.close()

    with TestClient(create_app(path)) as client:
        result = client.get("/v1/jobs").json()

    assert result["pending"] == 1
    assert result["jobs"][0]["stage"] == "queued"
    assert result["jobs"][0]["processed"] == 0
    assert result["jobs"][0]["total"] == 5
    assert result["jobs"][0]["heartbeat_at"] is None
