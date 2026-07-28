"""Dry-run extraction API tests."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.ingest.extractors import ExtractedClaim


class _DryRunExtractor:
    """Capture extraction inputs and expose deterministic token usage."""

    last_usage_tokens = 9
    last_input_tokens = 6
    last_output_tokens = 3

    def __init__(self) -> None:
        self.context: dict[str, Any] | None = None

    def extract(
        self,
        content: dict[str, Any] | str,
        context: dict[str, Any] | None = None,
    ) -> list[ExtractedClaim]:
        """Return one claim without touching persistence."""
        self.context = context
        return [ExtractedClaim(predicate="preference", value="dark mode")]


def test_dry_run_returns_claims_without_storing(tmp_path, monkeypatch) -> None:
    extractor = _DryRunExtractor()
    monkeypatch.setattr(server.components, "make_extractor", lambda *_args, **_kwargs: extractor)
    app = server.create_app(tmp_path / "dry-run.db")

    with TestClient(app) as client:
        before = client.get("/v1/stats").json()
        response = client.post("/v1/extract/dry-run", json={"text": "I prefer dark mode"})
        after = client.get("/v1/stats").json()

    assert response.status_code == 200
    assert response.json() == {
        "claims": [asdict(ExtractedClaim(predicate="preference", value="dark mode"))],
        "usage": {"total_tokens": 9, "input_tokens": 6, "output_tokens": 3},
    }
    assert after["claims"] == before["claims"]
    assert after["events"] == before["events"]


def test_dry_run_custom_instructions(tmp_path, monkeypatch) -> None:
    extractor = _DryRunExtractor()
    monkeypatch.setattr(server.components, "make_extractor", lambda *_args, **_kwargs: extractor)

    with TestClient(server.create_app(tmp_path / "dry-run-instructions.db")) as client:
        response = client.post(
            "/v1/extract/dry-run",
            json={
                "text": "I prefer dark mode",
                "context": {"project_id": "hl_mem"},
                "custom_instructions": "Only extract durable preferences.",
            },
        )

    assert response.status_code == 200
    assert extractor.context == {
        "project_id": "hl_mem",
        "custom_instructions": "Only extract durable preferences.",
    }
