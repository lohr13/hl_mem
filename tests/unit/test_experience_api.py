import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.deferred import process_recall_side_effect_tasks


def test_episode_api_supports_lifecycle_and_listing(tmp_path) -> None:
    app = create_app(tmp_path / "episodes.db")
    with TestClient(app) as client:
        created = client.post(
            "/v1/episodes",
            json={"goal": "修复部署", "session_id": "session-1", "task_type": "coding"},
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
        assert json.loads(detail["scope_json"]) == {
            "session_id": "session-1",
            "task_type": "coding",
        }
        assert [item["action"] for item in detail["traces"]] == ["运行测试"]

        assert client.get("/v1/episodes", params={"status": "success"}).json()["episodes"][0]["id"] == episode_id
        assert client.get("/v1/episodes", params={"status": "failed"}).json() == {"episodes": []}


def test_episode_api_returns_not_found(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        assert client.get("/v1/episodes/missing").status_code == 404
        assert client.patch("/v1/episodes/missing", json={"status": "failed"}).status_code == 404
        assert client.post("/v1/episodes/missing/traces", json={"action": "test"}).status_code == 404


def test_trace_observation_rejects_more_than_1000_characters(tmp_path) -> None:
    with TestClient(create_app(tmp_path / "trace-limit.db")) as client:
        episode_id = client.post("/v1/episodes", json={"goal": "test limit"}).json()["id"]

        accepted = client.post(
            f"/v1/episodes/{episode_id}/traces",
            json={"action": "test", "observation": "o" * 1000},
        )
        rejected = client.post(
            f"/v1/episodes/{episode_id}/traces",
            json={"action": "test", "observation": "o" * 1001},
        )

        assert accepted.status_code == 200
        assert rejected.status_code == 422


def test_policy_api_and_recall_attach_active_policies_for_task_queries(
    tmp_path,
) -> None:
    app = create_app(tmp_path / "policies.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        service = ExperienceService(connection, min_support=2)
        for episode_id in ("e1", "e2"):
            service.record_episode(episode_id, "修复故障", "success", 1.0, "2026-01-01T00:00:00Z")
        policy_id = service.induce_policy(
            "service outage",
            {"steps": ["inspect logs"]},
            ["e1", "e2"],
            "2026-01-02T00:00:00Z",
        )

        policies = client.get("/v1/policies").json()["policies"]
        assert [policy["id"] for policy in policies] == [policy_id]
        assert policies[0]["procedure"] == {"steps": ["inspect logs"]}
        assert client.get("/v1/policies", params={"status": "retired"}).json() == {"policies": []}

        assert client.post("/v1/recall", json={"query": "investigate service"}).json()["policies"][0]["id"] == policy_id
        assert client.post("/v1/recall", json={"query": "午餐偏好"}).json()["policies"] == []


def test_recall_records_impressions_and_feedback_updates_them(tmp_path) -> None:
    app = create_app(
        replace(
            Settings.for_test(),
            database_path=str(tmp_path / "feedback-api.db"),
            echo_suppression_mode="off",
            freshness_annotation_mode="off",
        )
    )
    with TestClient(app) as client:
        connection = app.state.db.open()
        ClaimRepository(connection).insert_claim(
            {
                "id": "claim-1",
                "status": "active",
                "subject_entity_id": "user",
                "predicate": "likes",
                "value": "tea",
                "index_text": "user likes tea",
                "recorded_from": "2026-07-22T00:00:00+00:00",
            }
        )

        recalled = client.post(
            "/v1/recall",
            json={
                "query": "likes tea",
                "limit": 1,
                "response_format": "both",
            },
        ).json()
        query_id = recalled["query_id"]
        assert app.state.recall_side_effects.drain(2.0)
        process_recall_side_effect_tasks(
            connection,
            now=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        )
        impression = connection.execute(
            "SELECT rank,score,injected,helpful FROM retrieval_feedback " "WHERE query_id=? AND memory_id='claim-1'",
            (query_id,),
        ).fetchone()
        assert impression[0] == 1
        assert impression[1] is not None
        assert impression[2] == 0
        assert impression[3] is None

        packet = recalled["context_packet"]
        assert packet["feedback_state"] == "available"
        feedback_id = packet["items"][0]["feedback_id"]
        assert recalled["results"][0]["feedback_id"] == feedback_id
        response = client.post(
            "/v1/feedback",
            json={"feedback_id": feedback_id, "helpful": True, "task_outcome": 1.0},
        )
        assert response.status_code == 200
        assert response.json()["updated"] is True
        stored = connection.execute(
            "SELECT helpful,task_outcome FROM retrieval_feedback WHERE query_id=? AND memory_id='claim-1'",
            (query_id,),
        ).fetchone()
        assert tuple(stored) == (1, 1.0)
