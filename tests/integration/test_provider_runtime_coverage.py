from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx

from hl_mem.components import (
    create_provider_runtime,
    make_embedder,
    make_image_describer,
    make_llm_client,
    make_reranker,
)
from hl_mem.domain.content import ImagePart
from hl_mem.llm.types import LLMMessage, LLMRequest
from hl_mem.settings import Settings

PNG = b"\x89PNG\r\n\x1a\n" + b"provider-runtime"


def _settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.for_test(),
        database_path=str(tmp_path / "memory.db"),
        extractor_mode="llm",
        llm_provider="openai_compatible",
        llm_api_key="llm-key",
        llm_base_url="https://provider.test/v1",
        llm_model="llm-model",
        llm_max_attempts=1,
        embedder_mode="real",
        embedding_api_key="embedding-key",
        embedding_base_url="https://provider.test/v1",
        embedding_model="embedding-model",
        embedding_dim=2,
        embedding_max_attempts=1,
        reranker_mode="real",
        reranker_api_key="reranker-key",
        reranker_base_url="https://provider.test",
        reranker_model="reranker-model",
        image_describer_mode="on",
        image_describer_api_key="image-key",
        image_describer_base_url="https://provider.test/v1",
        image_describer_model="image-model",
        image_max_bytes=128,
    )


def test_every_actual_provider_request_has_one_final_usage_event(tmp_path: Path) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                request=request,
                json={"data": [{"index": 0, "embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 2}},
            )
        if request.url.path.endswith("/text-rerank/text-rerank"):
            return httpx.Response(
                200,
                request=request,
                json={"output": {"results": [{"index": 0, "relevance_score": 0.9}]}},
            )
        content = body["messages"][-1]["content"]
        if isinstance(content, list):
            return httpx.Response(
                200,
                request=request,
                json={
                    "model": "image-model",
                    "choices": [{"message": {"content": '{"caption":"ok","ocr_text":"","confidence":null}'}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    settings = _settings(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=client)
    try:
        make_llm_client(settings, runtime=runtime).complete(LLMRequest([LLMMessage("user", "ping")]))
        make_embedder(settings, runtime=runtime).embed_one("ping")
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        reranker.rerank("ping", ["ping"], 1)
        image = ImagePart(None, base64.b64encode(PNG).decode(), "image/png")
        describer = make_image_describer(settings, runtime=runtime)
        assert describer is not None
        describer.describe(image, timeout_seconds=3.0)

        with sqlite3.connect(tmp_path / "memory.budget.db") as connection:
            finalized = int(connection.execute("SELECT count(*) FROM usage_events").fetchone()[0])
            active = int(
                connection.execute("SELECT count(*) FROM usage_reservations WHERE state='active'").fetchone()[0]
            )
        assert (attempts, finalized, active) == (4, 4, 0)
        assert runtime.governor.snapshot()["settled"]["requests"] == 4
    finally:
        runtime.close()
        client.close()


def test_disabled_and_fake_paths_create_zero_usage_events(tmp_path: Path) -> None:
    settings = replace(Settings.for_test(), database_path=str(tmp_path / "memory.db"))
    runtime = create_provider_runtime(settings)
    try:
        assert make_reranker(settings, runtime=runtime) is None
        assert make_image_describer(settings, runtime=runtime) is None
        assert runtime.governor.snapshot()["settled"]["requests"] == 0
    finally:
        runtime.close()
