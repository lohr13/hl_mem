from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.workers.worker import Worker


def test_explicit_memory_can_be_forgotten(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_EMBEDDER", "fake")
    app = create_app(tmp_path / "forget.db")
    with TestClient(app) as client:
        client.post("/v1/memories", json={"text": "秘密代号蓝鲸"})
        assert Worker(app.state.settings).run_once()["status"] == "succeeded"
        memory_id = app.state.db.open().execute("SELECT id FROM claims").fetchone()["id"]
        assert client.post("/v1/recall", json={"query": "蓝鲸"}).json()["total"] == 1
        assert client.delete(f"/v1/memories/{memory_id}").json()["forgotten"]
        assert client.post("/v1/recall", json={"query": "蓝鲸"}).json()["total"] == 0
        connection = app.state.db.open()
        assert connection.execute("SELECT 1 FROM claims WHERE id=?", (memory_id,)).fetchone() is None
        state = connection.execute(
            "SELECT last_identity_hash,last_applied_at FROM deletion_ledger_state WHERE singleton=1"
        ).fetchone()
        assert state["last_identity_hash"]
        assert state["last_applied_at"]
