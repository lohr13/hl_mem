from dataclasses import replace

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker


def test_fake_pipeline_filter_claim_evidence_recall_and_stats(tmp_path) -> None:
    app = create_app(tmp_path / "pipeline.db")
    with TestClient(app) as client:
        response = client.post(
            "/v1/events",
            json={
                "idempotency_key": "fact-1",
                "event_type": "message",
                "actor_type": "user",
                "content": {"text": "用户使用 PostgreSQL"},
            },
        )
        assert response.status_code == 200
        assert Worker(app.state.settings).run_once()["status"] == "succeeded"
        Worker(app.state.settings).run_once()  # process relation-discovery job
        recall = client.post("/v1/recall", json={"query": "PostgreSQL"}).json()
        assert recall["total"] == 1
        assert recall["results"][0]["evidence"]
        stats = client.get("/v1/stats").json()
        assert stats["events"] == 1
        assert stats["claims"] == 1
        assert stats["jobs_pending"] == 0


def test_filter_skips_extraction_and_job(tmp_path) -> None:
    app = create_app(tmp_path / "filtered.db")
    with TestClient(app) as client:
        client.post(
            "/v1/events",
            json={
                "event_type": "tool_result",
                "actor_type": "tool",
                "content": {"text": "command output"},
            },
        )
        assert Worker(app.state.settings).run_once()["claims"] == 0
        assert client.get("/v1/stats").json()["jobs_pending"] == 0


def test_exhausted_budget_leaves_job_pending(tmp_path) -> None:
    app = create_app(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "exhausted.db"),
            daily_token_limit=0,
        )
    )
    with TestClient(app) as client:
        client.post("/v1/events", json={"content": {"text": "用户使用 PostgreSQL"}})
        stats = client.get("/v1/stats").json()
        assert stats["claims"] == 0
        assert stats["jobs_pending"] == 1
