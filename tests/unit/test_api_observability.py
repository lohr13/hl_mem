"""FastAPI health and request lifecycle observability tests."""

from __future__ import annotations

import inspect
import logging
import re
import sqlite3
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.observability.usage_types import UsageAmount, UsageIdentity
from hl_mem.plugins.contracts import ProviderCapability
from hl_mem.settings import Settings


def _route_endpoint(app: FastAPI, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def _request_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "hl_mem.api.server" and record.getMessage().startswith("request_")
    ]


def test_healthz_endpoint_is_async(tmp_path) -> None:
    app = create_app(tmp_path / "health-async.db")

    assert inspect.iscoroutinefunction(_route_endpoint(app, "/healthz"))


def test_healthz_provider_inventory_is_bounded_and_secret_free(tmp_path) -> None:
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / "health-providers.db"),
        embedder_mode="real",
        embedding_api_key="never-expose-this-key",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/healthz")

    providers = response.json()["providers"]
    assert providers
    assert all(set(item) == {"plugin_id", "capability", "name", "stability", "health"} for item in providers)
    serialized = response.text
    assert "never-expose-this-key" not in serialized
    assert settings.embedding_base_url not in serialized


def test_healthz_provider_usage_preserves_detail_and_adds_daily_health(tmp_path) -> None:
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / "health-usage.db"),
        embedder_mode="real",
        embedding_api_key="test-key",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        usage = client.get("/healthz").json()["provider_usage"]

    assert usage is not None
    assert set(usage) == {
        "date",
        "settled",
        "reserved",
        "remaining",
        "unknown_cost_count",
        "counts_by_capability",
        "health",
    }
    assert usage["health"] == {
        "failures": 0,
        "stale_reservations": 0,
        "utilization": {"requests": None, "tokens": "0", "cost_microunits": None},
        "unknown_outcomes": 0,
        "unknown_costs": 0,
    }


@pytest.mark.parametrize("lease_expires_at", ["not-an-iso-timestamp", "2026-08-30T13:00:01.000000"])
def test_healthz_degrades_invalid_or_naive_usage_lease_without_leaking_details(
    tmp_path,
    lease_expires_at: str,
) -> None:
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / "health-invalid-lease.db"),
        embedder_mode="real",
        embedding_api_key="test-key",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        runtime = app.state.provider_runtime
        reservation = runtime.governor.reserve(
            UsageIdentity(
                ProviderCapability.EMBEDDING,
                "embed",
                "hl-mem.builtin",
                "openai_compatible",
                "test-model",
            ),
            UsageAmount(requests=7),
        )
        ledger_path = runtime.governor.path
        with sqlite3.connect(ledger_path) as connection:
            connection.execute(
                "UPDATE usage_reservations SET lease_expires_at=? WHERE id=?",
                (lease_expires_at, reservation.id),
            )

        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    usage = response.json()["provider_usage"]
    assert set(usage) == {
        "date",
        "settled",
        "reserved",
        "remaining",
        "unknown_cost_count",
        "counts_by_capability",
        "health",
    }
    assert usage["health"] is None
    assert lease_expires_at not in response.text
    assert str(ledger_path) not in response.text
    assert "user-defined function raised exception" not in response.text


def test_request_lifecycle_logs_healthz_with_query_id(caplog, tmp_path) -> None:
    app = create_app(tmp_path / "health-logging.db")

    with caplog.at_level(logging.INFO, logger="hl_mem.api.server"):
        with TestClient(app) as client:
            response = client.get("/healthz", headers={"X-Request-ID": "query-123"})

    assert response.status_code == 200
    messages = _request_messages(caplog)
    assert messages[0] == "request_started method=GET path=/healthz query_id=query-123"
    assert re.fullmatch(
        r"request_finished method=GET path=/healthz status=200 duration_ms=\d+\.\d{3}",
        messages[1],
    )


def test_request_lifecycle_logs_finish_when_handler_raises(caplog, tmp_path) -> None:
    app = create_app(tmp_path / "failed-request-logging.db")

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("simulated handler failure")

    with caplog.at_level(logging.INFO, logger="hl_mem.api.server"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/explode")

    assert response.status_code == 500
    messages = _request_messages(caplog)
    assert messages[0] == "request_started method=GET path=/explode"
    assert re.fullmatch(
        r"request_finished method=GET path=/explode status=500 duration_ms=\d+\.\d{3}",
        messages[1],
    )


def test_request_lifecycle_log_bounds_query_id(caplog, tmp_path) -> None:
    app = create_app(tmp_path / "bounded-request-id.db")

    with caplog.at_level(logging.INFO, logger="hl_mem.api.server"):
        with TestClient(app) as client:
            response = client.get("/healthz", headers={"X-Request-ID": "x" * 500})

    assert response.status_code == 200
    started = _request_messages(caplog)[0]
    assert started == f"request_started method=GET path=/healthz query_id={'x' * 200}"
