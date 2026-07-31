from __future__ import annotations

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app


def test_memory_api_idempotency_body_conflict_and_unkeyed_semantics(tmp_path) -> None:
    app = create_app(tmp_path / "memory-api.db")
    with TestClient(app) as client:
        payload = {
            "text": "记住 SQLite",
            "namespace": "project-a",
            "idempotency_key": "memory-body-1",
            "qualifiers": {"source": "api"},
        }
        first = client.post("/v1/memories", json=payload)
        duplicate = client.post("/v1/memories", json=payload)
        conflict = client.post(
            "/v1/memories",
            json={**payload, "text": "改用 PostgreSQL"},
        )
        unkeyed_first = client.post(
            "/v1/memories",
            json={"text": "允许重复", "namespace": "project-a"},
        )
        unkeyed_second = client.post(
            "/v1/memories",
            json={"text": "允许重复", "namespace": "project-a"},
        )

        assert first.status_code == 200
        assert first.json()["created"] is True
        assert duplicate.json() == {"id": first.json()["id"], "created": False}
        assert conflict.status_code == 409
        assert unkeyed_first.json()["created"] is True
        assert unkeyed_second.json()["created"] is True
        assert unkeyed_first.json()["id"] != unkeyed_second.json()["id"]

        connection = app.state.db.open()
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
        stored = connection.execute(
            "SELECT tenant_id FROM events WHERE id=?",
            (first.json()["id"],),
        ).fetchone()
        assert stored[0] == "project-a"


def test_memory_api_header_key_wins_and_is_bounded(tmp_path) -> None:
    app = create_app(tmp_path / "memory-header.db")
    with TestClient(app) as client:
        first = client.post(
            "/v1/memories",
            headers={"Idempotency-Key": "host-retry"},
            json={"text": "稳定请求", "idempotency_key": "body-one"},
        )
        duplicate = client.post(
            "/v1/memories",
            headers={"Idempotency-Key": "host-retry"},
            json={"text": "稳定请求", "idempotency_key": "body-two"},
        )
        oversized = client.post(
            "/v1/memories",
            headers={"Idempotency-Key": "x" * 201},
            json={"text": "不会写入"},
        )

        assert duplicate.json() == {"id": first.json()["id"], "created": False}
        assert oversized.status_code == 422
        connection = app.state.db.open()
        row = connection.execute("SELECT idempotency_key FROM events").fetchone()
        assert row[0] == "host-retry"


def test_namespace_alias_conflicts_and_episode_lists_are_isolated(tmp_path) -> None:
    app = create_app(tmp_path / "namespace-api.db")
    with TestClient(app) as client:
        legacy_event = client.post(
            "/v1/events",
            json={"tenant_id": "legacy", "content": "兼容请求"},
        )
        conflicting_event = client.post(
            "/v1/events",
            json={
                "namespace": "project-a",
                "tenant_id": "project-b",
                "content": "含糊请求",
            },
        )
        episode_a = client.post(
            "/v1/episodes",
            json={"goal": "任务 A", "namespace": "project-a"},
        )
        episode_b = client.post(
            "/v1/episodes",
            json={"goal": "任务 B", "namespace": "project-b"},
        )

        assert legacy_event.status_code == 200
        assert conflicting_event.status_code == 422
        assert episode_a.status_code == 200
        assert episode_b.status_code == 200
        assert [
            item["id"] for item in client.get("/v1/episodes", params={"namespace": "project-a"}).json()["episodes"]
        ] == [episode_a.json()["id"]]
        assert [
            item["id"] for item in client.get("/v1/episodes", params={"tenant_id": "project-b"}).json()["episodes"]
        ] == [episode_b.json()["id"]]
        assert (
            client.get(
                "/v1/episodes",
                params={"namespace": "project-a", "tenant_id": "project-b"},
            ).status_code
            == 422
        )

        connection = app.state.db.open()
        stored = connection.execute(
            "SELECT tenant_id FROM events WHERE id=?",
            (legacy_event.json()["id"],),
        ).fetchone()
        assert stored[0] == "legacy"
