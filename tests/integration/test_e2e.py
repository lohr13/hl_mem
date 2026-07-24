from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
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
    app = create_app(tmp_path / "e2e.db")
    with TestClient(app) as client:
        first = client.post("/v1/events", json=event("key-1", "s1", "我喜欢 PostgreSQL"))
        duplicate = client.post("/v1/events", json=event("key-1", "s1", "我喜欢 PostgreSQL"))
        client.post("/v1/events", json=event("key-2", "s2", "记住 PostgreSQL 开启备份"))
        worker = Worker(tmp_path / "e2e.db")
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
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_data_survives_database_restart(tmp_path) -> None:
    path = tmp_path / "restart.db"
    with TestClient(create_app(path)) as client:
        client.post("/v1/events", json=event("persist-1", "s1", "记住使用 SQLite 持久化"))
    assert Worker(path).run_once()["status"] == "succeeded"
    with TestClient(create_app(path)) as client:
        response = client.post("/v1/recall", json={"query": "SQLite", "session_id": "s2"})
        assert response.json()["total"] == 1
        assert response.json()["results"][0]["evidence"]


def test_healthz(tmp_path) -> None:
    from hl_mem import __version__

    with TestClient(create_app(tmp_path / "health.db")) as client:
        result = client.get("/healthz").json()
        assert result["status"] == "ok"
        assert result["version"] == __version__
        assert "embedder" in result
        assert "reranker" in result
        assert result["llm_stats"] == {"calls": 0, "total_tokens": 0}
