from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.workers.worker import Worker


def test_preference_state_change_and_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_EMBEDDER", "fake")
    with TestClient(create_app(tmp_path / "conflict.db")) as client:
        client.post("/v1/events", json={"content": {"text": "我喜欢深色模式"},
                                         "occurred_at": "2026-01-01T00:00:00+00:00"})
        client.post("/v1/events", json={"content": {"text": "现在用浅色模式"},
                                         "occurred_at": "2026-02-01T00:00:00+00:00"})
        worker = Worker(tmp_path / "conflict.db")
        worker.run_once()
        worker.run_once()
        current = client.post("/v1/recall", json={"query": "模式偏好"}).json()["results"]
        assert [item["text"] for item in current if item["type"] == "claim"] == ["浅色模式"]
        history = client.post("/v1/recall", json={"query": "深色", "as_of": "2026-01-15T00:00:00+00:00"}).json()
        assert any(item.get("text") == "深色模式" for item in history["results"])
