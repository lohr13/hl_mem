"""Context Packet 预算、receipt 与显式 feedback 的跨层闭环。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app


def _seed_claims(connection) -> None:
    connection.executemany(
        "INSERT INTO claims("
        "id,status,subject_entity_id,predicate,value_json,index_text,recorded_from"
        ") VALUES (?,?,?,?,?,?,?)",
        (
            (
                "claim-tea",
                "active",
                "user",
                "likes",
                '"SECRET_RAW_VALUE"',
                "likes tea",
                "2026-07-31T00:00:00+00:00",
            ),
            (
                "claim-milk",
                "active",
                "user",
                "likes",
                '"another raw value"',
                "likes tea with milk every morning",
                "2026-07-31T00:00:01+00:00",
            ),
        ),
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation,weight"
        ") VALUES (?,?,?,?,?,?,?)",
        (
            "evidence-1",
            "claim",
            "claim-tea",
            "event",
            "event-1",
            "supports",
            1.0,
        ),
    )
    connection.commit()


def test_packet_materializes_only_budgeted_items_and_fresh_receipts(tmp_path) -> None:
    app = create_app(tmp_path / "packet-feedback.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        _seed_claims(connection)

        legacy = client.post(
            "/v1/recall",
            json={"query": "likes tea", "limit": 2},
            headers={"X-Request-ID": "legacy-query"},
        ).json()
        assert "context_packet" not in legacy
        assert legacy["results"]
        assert all(item["feedback_id"] for item in legacy["results"])
        legacy_exposures = connection.execute(
            "SELECT count(*) FROM retrieval_feedback WHERE query_id='legacy-query'"
        ).fetchone()[0]
        assert legacy_exposures == len(legacy["results"])

        request = {
            "query": "likes tea",
            "limit": 2,
            "token_budget": 5,
            "response_format": "context_packet",
        }
        first_response = client.post(
            "/v1/recall",
            json=request,
            headers={"X-Request-ID": "cached-query"},
        )
        assert first_response.status_code == 200
        assert set(first_response.json()) == {"context_packet"}
        first = first_response.json()["context_packet"]
        assert first["query_id"] == "cached-query"
        assert first["feedback_state"] == "available"
        assert first["truncated"] is True
        assert first["used_tokens_estimate"] == 5
        assert first["items"] == [
            {
                "type": "claim",
                "id": "claim-tea",
                "text": "likes tea",
                "evidence": [{"type": "event", "id": "event-1"}],
                "feedback_id": first["items"][0]["feedback_id"],
            }
        ]
        assert "SECRET_RAW_VALUE" not in first["items"][0]["text"]

        second = client.post(
            "/v1/recall",
            json=request,
            headers={"X-Request-ID": "cached-query"},
        ).json()["context_packet"]
        assert second["query_id"] == first["query_id"]
        assert second["items"][0]["id"] == first["items"][0]["id"]
        assert second["items"][0]["feedback_id"] != first["items"][0]["feedback_id"]

        rows = connection.execute(
            "SELECT query_id,memory_id,rank,injected,helpful,task_outcome "
            "FROM retrieval_feedback WHERE query_id='cached-query' ORDER BY created_at,id"
        ).fetchall()
        assert len(rows) == 2
        assert {tuple(row) for row in rows} == {
            ("cached-query", "claim-tea", 1, 0, None, None),
        }

        feedback_id = first["items"][0]["feedback_id"]
        submitted = client.post(
            "/v1/feedback",
            json={"feedback_id": feedback_id, "helpful": True, "task_outcome": 1.0},
        )
        replayed = client.post(
            "/v1/feedback",
            json={"feedback_id": feedback_id, "helpful": True, "task_outcome": 1.0},
        )
        assert submitted.json()["updated"] is True
        assert replayed.json() == {"created": False, "updated": False}

        unknown = client.post(
            "/v1/feedback",
            json={"feedback_id": "unknown-feedback", "helpful": True},
        )
        assert unknown.status_code == 404
        assert "feedback exposure not found" in unknown.json()["detail"]
