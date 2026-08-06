"""向量检索后端协议、配置和健康指标测试。"""

from __future__ import annotations

import builtins
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.ingest import IngestService
from hl_mem.domain.temporal import RecallIntent
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.protocols import ClaimRow, VectorSearchBackend
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.sqlite_vec import load_sqlite_vec_extension


def _search(backend: VectorSearchBackend) -> list[ClaimRow]:
    """通过协议调用向量检索后端。"""
    return backend.search(
        b"",
        5,
        "2026-07-24T00:00:00+00:00",
        RecallIntent.CURRENT_STATE,
        None,
        "default",
    )


def test_vector_backend_protocol_accepts_repository(tmp_path: Path) -> None:
    connection = Database(tmp_path / "protocol.db").open()

    assert _search(ClaimRepository(connection)) == []


def test_vector_backend_config_default() -> None:
    assert Settings().vector_backend == "sqlite_scan"


def test_vector_backend_config_rejects_unknown_value() -> None:
    settings = replace(Settings.for_test(), vector_backend="unknown")  # type: ignore[arg-type]

    with pytest.raises(ConfigurationError, match="recall.vector_backend"):
        settings.validate()


def test_default_scan_database_does_not_import_or_create_sqlite_vec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def reject_sqlite_vec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sqlite_vec":
            raise AssertionError("default sqlite_scan must not import sqlite_vec")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_sqlite_vec)
    database = Database(tmp_path / "scan-only.db")
    connection = database.open()
    try:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='claims_vec_v1'").fetchone() is None
    finally:
        database.close()


def test_sqlite_vec_loader_explains_missing_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def hide_sqlite_vec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "sqlite_vec":
            raise ImportError("sqlite_vec hidden for contract test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", hide_sqlite_vec)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 45, 0))
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ConfigurationError) as captured:
            load_sqlite_vec_extension(connection)
    finally:
        connection.close()

    message = str(captured.value)
    assert "optional dependency 'sqlite-vec' is not installed" in message
    assert "hl-mem[sqlite-vec]" in message
    assert "sqlite_scan" in message


def test_healthz_reports_last_embedded_candidate_count(tmp_path: Path) -> None:
    database_path = tmp_path / "health.db"
    connection = Database(database_path).open()
    IngestService.store_extracted(
        connection,
        ExtractedClaim("事实", "向量候选"),
        {"id": "event-vector", "actor_type": "user"},
        "2026-07-24T00:00:00+00:00",
        FakeEmbedder(2048),
    )

    with TestClient(create_app(database_path)) as client:
        recall = client.post("/v1/recall", json={"query": "向量候选"})
        health = client.get("/healthz")

    assert recall.status_code == 200
    assert health.json()["vector_backend"] == "sqlite_scan"
    assert health.json()["vector_search"]["embedded_candidate_count"] == 1
