from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.ingest.embedder import Embedder
from hl_mem.ingest.image_describer import GovernedImageDescriber
from hl_mem.llm.client import LLMClient
from hl_mem.recall.reranker import DashScopeReranker
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker


def test_default_runtime_and_maintenance_make_zero_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
        calls.append("provider")
        raise AssertionError("default path invoked a model provider")

    monkeypatch.setattr(LLMClient, "complete", unexpected_call)
    monkeypatch.setattr(Embedder, "embed_batch", unexpected_call)
    monkeypatch.setattr(DashScopeReranker, "rerank", unexpected_call)
    monkeypatch.setattr(GovernedImageDescriber, "describe", unexpected_call)
    settings = replace(Settings.for_test(), database_path=str(tmp_path / "zero-model-calls.db"))

    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/recall", json={"query": "absent synthetic fact"})
        assert response.status_code == 200
        worker = Worker(settings)
        worker._run_maintenance()
        worker.close()

    assert calls == []
