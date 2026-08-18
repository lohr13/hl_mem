from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app


def test_delete_memory_returns_404_for_unknown_claim(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "forget-missing.db"), raise_server_exceptions=False) as client:
        response = client.delete("/v1/memories/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "memory not found"}
