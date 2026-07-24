import json

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.experience.service import ExperienceService


def test_episode_api_supports_lifecycle_and_listing(tmp_path) -> None:
    app = create_app(tmp_path / "episodes.db")
    with TestClient(app) as client:
        created = client.post(
            "/v1/episodes", json={"goal": "修复部署", "session_id": "session-1", "task_type": "coding"}
        )
        assert created.status_code == 200
        episode_id = created.json()["id"]

        trace = client.post(
            f"/v1/episodes/{episode_id}/traces",
            json={"action": "运行测试", "observation": "通过", "value": 0.8},
        )
        assert trace.status_code == 200
        assert trace.json()["episode_id"] == episode_id

        updated = client.patch(
            f"/v1/episodes/{episode_id}",
            json={"status": "success", "reward": 0.8, "outcome_summary": "部署完成"},
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "success"
        assert updated.json()["traces"][0]["value"] == 0.8

        detail = client.get(f"/v1/episodes/{episode_id}").json()
        assert detail["goal"] == "修复部署"
        assert json.loads(detail["scope_json"]) == {"session_id": "session-1", "task_type": "coding"}
        assert [item["action"] for item in detail["traces"]] == ["运行测试"]

        assert client.get("/v1/episodes", params={"status": "success"}).json()["episodes"][0]["id"] == episode_id
        assert client.get("/v1/episodes", params={"status": "failed"}).json() == {"episodes": []}


def test_episode_api_returns_not_found(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        assert client.get("/v1/episodes/missing").status_code == 404
        assert client.patch("/v1/episodes/missing", json={"status": "failed"}).status_code == 404
        assert client.post("/v1/episodes/missing/traces", json={"action": "test"}).status_code == 404


def test_policy_api_and_recall_attach_active_policies_for_task_queries(tmp_path) -> None:
    app = create_app(tmp_path / "policies.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        service = ExperienceService(connection, min_support=2)
        for episode_id in ("e1", "e2"):
            service.record_episode(episode_id, "修复故障", "success", 1.0, "2026-01-01T00:00:00Z")
        policy_id = service.induce_policy(
            "service outage", {"steps": ["inspect logs"]}, ["e1", "e2"], "2026-01-02T00:00:00Z"
        )

        policies = client.get("/v1/policies").json()["policies"]
        assert [policy["id"] for policy in policies] == [policy_id]
        assert policies[0]["procedure"] == {"steps": ["inspect logs"]}
        assert client.get("/v1/policies", params={"status": "retired"}).json() == {"policies": []}

        assert client.post("/v1/recall", json={"query": "investigate service"}).json()["policies"][0]["id"] == policy_id
        assert client.post("/v1/recall", json={"query": "午餐偏好"}).json()["policies"] == []


def test_recall_records_impressions_and_feedback_updates_them(tmp_path) -> None:
    app = create_app(tmp_path / "feedback-api.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        connection.execute(
            "INSERT INTO claims(id,status,subject_entity_id,predicate,value_json,recorded_from) "
            "VALUES ('claim-1','active','user','likes','\"tea\"','2026-07-22T00:00:00+00:00')"
        )
        connection.commit()

        recalled = client.post("/v1/recall", json={"query": "likes tea", "limit": 1}).json()
        query_id = recalled["query_id"]
        impression = connection.execute(
            "SELECT rank,score,helpful FROM retrieval_feedback WHERE query_id=? AND memory_id='claim-1'",
            (query_id,),
        ).fetchone()
        assert impression[0] == 1
        assert impression[1] is not None
        assert impression[2] is None

        response = client.post(
            "/v1/feedback",
            json={"query_id": query_id, "memory_id": "claim-1", "helpful": True, "task_outcome": "success"},
        )
        assert response.status_code == 200
        assert response.json()["updated"] is True
        stored = connection.execute(
            "SELECT helpful,task_outcome FROM retrieval_feedback WHERE query_id=? AND memory_id='claim-1'",
            (query_id,),
        ).fetchone()
        assert tuple(stored) == (1, "success")
