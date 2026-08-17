"""Hermes receipt-free prefetch 到 injected delivery 的真实 daemon 闭环。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from hl_mem import __version__
from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.api.server import create_app
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.deferred import process_recall_side_effect_tasks


def _settle_recall_side_effects(app, connection) -> None:
    assert app.state.recall_side_effects.drain(2.0)
    process_recall_side_effect_tasks(
        connection,
        now=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    )


def _seed_claim(connection) -> None:
    ClaimRepository(connection).insert_claim(
        {
            "id": "claim-tea",
            "status": "active",
            "subject_entity_id": "user",
            "predicate": "likes",
            "value_json": '"SECRET_RAW_VALUE"',
            "index_text": "likes tea",
            "qualifiers": {"role": "user", "action": "likes", "object": "tea"},
            "recorded_from": "2026-07-31T00:00:00+00:00",
        },
        commit=False,
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

        expected_text = "likes tea\nrelation: user → likes → tea"
        assert first_text == second_text == expected_text
        assert "SECRET_RAW_VALUE" not in first_text
        assert "SECRET_RAW_VALUE" not in second_text
        assert provider.health()["delivery"]["pending_injections"] == 2
        assert provider.flush_delivery_receipts() == 2
        _settle_recall_side_effects(app, connection)
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


def test_two_turn_query_miss_recalls_on_demand_materializes_and_marks_injected(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "hermes-two-turn.db")
    with TestClient(app) as daemon:
        connection = app.state.db.open()
        _seed_claim(connection)
        requests = []

        def daemon_post(
            url: str,
            *,
            json: dict,
            timeout: float,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            del timeout
            path = urlsplit(url).path
            requests.append((path, json))
            return daemon.post(path, json=json, headers=headers)

        monkeypatch.setattr(httpx, "post", daemon_post)
        provider = HLMemProvider(settings=Settings(hermes_url="http://memory.test", hermes_timeout=2))
        request = {
            "session_id": "session-1",
            "limit": 1,
            "intent": "current_state",
            "namespace": "default",
            "token_budget": 100,
        }

        provider.queue_prefetch("previous turn topic", **request)
        provider._prefetch_cache.drain(2.0)  # noqa: SLF001
        rendered = provider.prefetch("likes tea", **request)

        assert rendered == "likes tea\nrelation: user → likes → tea"
        bundle_queries = [payload["query"] for path, payload in requests if path == "/v1/internal/retrieval-bundles"]
        assert bundle_queries == ["previous turn topic", "likes tea"]
        assert sum(path == "/v1/internal/context-packets/materialize" for path, _ in requests) == 1
        _settle_recall_side_effects(app, connection)
        assert connection.execute("SELECT injected FROM retrieval_feedback").fetchone()[0] == 0

        assert provider.flush_delivery_receipts() == 1
        assert connection.execute("SELECT injected FROM retrieval_feedback").fetchone()[0] == 1
        health = provider.health()
        assert health["prefetch_failures"] == 0
        assert health["injection_successes"] == 1


def test_prefetch_truncates_query_to_recall_input_contract(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "hermes-query-boundary.db")
    with TestClient(app) as daemon:
        sent_queries = []

        def daemon_post(
            url: str,
            *,
            json: dict,
            timeout: float,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            del timeout
            path = urlsplit(url).path
            if path == "/v1/internal/retrieval-bundles":
                sent_queries.append(json["query"])
            return daemon.post(path, json=json, headers=headers)

        monkeypatch.setattr(httpx, "post", daemon_post)
        provider = HLMemProvider(settings=Settings(hermes_url="http://memory.test", hermes_timeout=2))
        long_query = "q" * 2_500

        provider.queue_prefetch(long_query, session_id="session-1")
        provider._prefetch_cache.drain(2.0)  # noqa: SLF001

        budget = provider.settings.packed_context_token_budget
        entry = provider._prefetch_cache.inspect(  # noqa: SLF001
            "session-1",
            long_query,
            token_budget=budget,
        )
        assert entry is not None
        assert entry.status == "completed"
        assert sent_queries == ["q" * 2_000]


def test_daemon_validation_response_reports_server_version(tmp_path) -> None:
    app = create_app(tmp_path / "hermes-version-header.db")
    with TestClient(app) as daemon:
        response = daemon.post(
            "/v1/internal/retrieval-bundles",
            json={"query": "q" * 2_001},
        )

        assert response.status_code == 422
        assert response.headers["X-HL-Mem-Version"] == __version__
