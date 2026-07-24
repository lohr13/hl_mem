"""向量检索后端协议、配置和健康指标测试。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.ingest import IngestService
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.protocols import VectorSearchBackend
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


def _search(backend: VectorSearchBackend) -> list[dict]:
    """通过协议调用向量检索后端。"""
    return backend.search(b"", 5, "2026-07-24T00:00:00+00:00", None, None, "default")


def test_vector_backend_protocol_accepts_repository(tmp_path) -> None:
    connection = Database(tmp_path / "protocol.db").open()

    assert _search(ClaimRepository(connection)) == []


def test_vector_backend_config_default(monkeypatch: Any) -> None:
    monkeypatch.delenv("HL_MEM_VECTOR_BACKEND", raising=False)

    assert Settings.from_env().vector_backend == "sqlite_scan"


def test_healthz_reports_last_embedded_candidate_count(tmp_path) -> None:
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
    assert health.json()["vector_search"]["embedded_candidate_count"] == 1
