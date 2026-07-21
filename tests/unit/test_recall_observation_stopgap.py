from fastapi.testclient import TestClient

from hl_mem.api.server import create_app


def test_recall_never_returns_active_observations(tmp_path) -> None:
    app = create_app(tmp_path / "recall-stopgap.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        connection.execute(
            "INSERT INTO derivations(id,kind,body,status,confidence,updated_at) "
            "VALUES ('observation-1','observation','不应召回','active',0.9,'2026-07-21T00:00:00+00:00')"
        )
        connection.commit()

        response = client.post("/v1/recall", json={"query": "任意查询"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["observations"] == []
    assert payload["results"] == []
    assert payload["total"] == 0
