"""Hermes receipt-free prefetch 到 injected delivery 的真实 daemon 闭环。"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.api.server import create_app
from hl_mem.settings import Settings


def _seed_claim(connection) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,status,subject_entity_id,predicate,value_json,index_text,recorded_from"
        ") VALUES (?,?,?,?,?,?,?)",
        (
            "claim-tea",
            "active",
            "user",
            "likes",
            '"SECRET_RAW_VALUE"',
            "likes tea",
            "2026-07-31T00:00:00+00:00",
        ),
    )
    connection.commit()


def test_prefetch_is_receipt_free_and_each_delivery_is_fresh_and_injected(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "hermes-delivery.db")
    with TestClient(app) as daemon:
        connection = app.state.db.open()
        _seed_claim(connection)

        def daemon_post(
            url: str,
            *,
            json: dict,
            timeout: float,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            del timeout
            return daemon.post(
                urlsplit(url).path,
                json=json,
                headers=headers,
            )

        monkeypatch.setattr(httpx, "post", daemon_post)
        provider = HLMemProvider(
            settings=Settings(
                hermes_url="http://memory.test",
                hermes_timeout=2,
            )
        )
        request = {
            "session_id": "session-1",
            "limit": 1,
            "intent": "current_state",
            "namespace": "default",
            "token_budget": 100,
        }

        provider.queue_prefetch("likes tea", **request)
        provider._prefetch_cache.drain(2.0)  # noqa: SLF001

        assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 0
        cached = provider._prefetch_cache.get(  # noqa: SLF001
            "session-1",
            "likes tea",
            limit=1,
            intent="current_state",
            namespace="default",
            token_budget=100,
        )
        assert cached is not None
        assert cached.query_id
        assert cached.items[0].text == "likes tea"
        assert not hasattr(cached.items[0], "feedback_id")

        first_text = provider.prefetch("likes tea", **request)
        second_text = provider.prefetched("likes tea", **request)

        assert first_text == second_text == "likes tea"
        assert provider.health()["delivery"]["pending_injections"] == 2
        assert {row[0] for row in connection.execute("SELECT injected FROM retrieval_feedback").fetchall()} == {0}
        assert provider.flush_delivery_receipts() == 2
        receipts = provider.delivery_receipts
        assert len(receipts) == 2
        assert receipts[0].query_id == receipts[1].query_id == cached.query_id
        assert receipts[0].feedback_ids != receipts[1].feedback_ids
        assert all(receipt.feedback_ids for receipt in receipts)

        rows = connection.execute(
            "SELECT id,query_id,memory_id,rank,injected,helpful,task_outcome "
            "FROM retrieval_feedback ORDER BY created_at,id"
        ).fetchall()
        assert len(rows) == 2
        assert {row["query_id"] for row in rows} == {cached.query_id}
        assert {row["memory_id"] for row in rows} == {"claim-tea"}
        assert {row["rank"] for row in rows} == {1}
        assert {row["injected"] for row in rows} == {1}
        assert {row["helpful"] for row in rows} == {None}
        assert {row["task_outcome"] for row in rows} == {None}

        submitted = daemon.post(
            "/v1/feedback",
            json={
                "feedback_id": receipts[0].feedback_ids[0],
                "helpful": True,
                "task_outcome": 1.0,
            },
        )
        assert submitted.status_code == 200
        assert submitted.json()["updated"] is True
