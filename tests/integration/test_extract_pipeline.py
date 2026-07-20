from fastapi.testclient import TestClient

from hl_mem.api.server import create_app


def test_fake_pipeline_filter_claim_evidence_recall_and_stats(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_EXTRACTOR", "fake")
    app = create_app(tmp_path / "pipeline.db")
    with TestClient(app) as client:
        response = client.post("/v1/events", json={
            "idempotency_key": "fact-1", "event_type": "message", "actor_type": "user",
            "content": {"text": "用户使用 PostgreSQL"},
        })
        assert response.status_code == 200
        recall = client.post("/v1/recall", json={"query": "PostgreSQL"}).json()
        assert recall["total"] == 1
        assert recall["results"][0]["evidence"]
        stats = client.get("/v1/stats").json()
        assert stats == {"events": 1, "claims": 1, "tokens_today": 0, "jobs_pending": 1}


def test_filter_skips_extraction_and_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_EXTRACTOR", "fake")
    app = create_app(tmp_path / "filtered.db")
    with TestClient(app) as client:
        client.post("/v1/events", json={
            "event_type": "tool_result", "actor_type": "tool",
            "content": {"text": "command output"},
        })
        assert client.get("/v1/stats").json()["jobs_pending"] == 0


def test_exhausted_budget_leaves_job_pending(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_EXTRACTOR", "fake")
    monkeypatch.setenv("HL_MEM_DAILY_TOKEN_LIMIT", "0")
    app = create_app(tmp_path / "exhausted.db")
    with TestClient(app) as client:
        client.post("/v1/events", json={"content": {"text": "用户使用 PostgreSQL"}})
        stats = client.get("/v1/stats").json()
        assert stats["claims"] == 0
        assert stats["jobs_pending"] == 1
