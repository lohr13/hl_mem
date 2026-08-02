"""FastAPI health and request lifecycle observability tests."""

from __future__ import annotations

import inspect
import logging
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app


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
