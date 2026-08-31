from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hl_mem.components import create_provider_runtime, make_embedder
from hl_mem.errors import ProviderCallError, UsageLimitExceededError
from hl_mem.settings import Settings
from tests.unit._usage_pricing_fixture import write_usage_price_book

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "providers" / "embedding_dashscope.json"


def _settings(tmp_path: Path, **changes: object) -> Settings:
    settings = replace(
        Settings.for_test(),
        database_path=str(tmp_path / "memory.db"),
        embedder_mode="real",
        embedding_provider="dashscope",
        embedding_api_key="test-key",
        embedding_base_url="https://example.test/compatible-mode/v1",
        embedding_model="text-embedding-v4",
        embedding_dim=2,
        embedding_max_attempts=1,
    )
    return replace(settings, **changes)


@pytest.mark.parametrize("mode", ("compatible", "native"))
def test_registry_embedding_matches_frozen_wire_contract(tmp_path: Path, mode: str) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[mode]
    captured: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "json": json.loads(request.content)})
        return httpx.Response(200, request=request, json=fixture["response"])

    changes: dict[str, object] = {"embedding_api_mode": mode}
    if mode == "native":
        changes.update(
            embedding_base_url="https://example.test",
            embedding_model="qwen3.7-text-embedding",
            embedding_text_type="document",
        )
    settings = _settings(tmp_path, **changes)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        blobs = make_embedder(settings, runtime=runtime).embed_batch(["first", "second"])
        assert [struct.unpack("<2f", blob) for blob in blobs] == [(1.0, 2.0), (3.0, 4.0)]
        assert captured == [{"url": fixture["url"], "json": fixture["request"]}]
        settled = runtime.governor.snapshot()["settled"]
        assert settled["requests"] == 1
        assert settled["embedding_items"] == 2
        assert settled["input_tokens"] == 4
    finally:
        runtime.close()
        http_client.close()


def test_eleven_embeddings_create_two_usage_events_not_three(tmp_path: Path) -> None:
    batch_sizes: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        batch_sizes.append(len(texts))
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"index": index, "embedding": [1.0, 0.0]} for index, _ in enumerate(texts)]},
        )

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        assert len(make_embedder(settings, runtime=runtime).embed_batch([str(index) for index in range(11)])) == 11
        settled = runtime.governor.snapshot()["settled"]
        assert settled["requests"] == 2
        assert settled["embedding_items"] == 11
        assert batch_sizes == [10, 1]
        with sqlite3.connect(tmp_path / "memory.budget.db") as connection:
            assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 2
            assert {row[0] for row in connection.execute("SELECT status FROM usage_events")} == {"usage_unknown"}
    finally:
        runtime.close()
        http_client.close()


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"data": [{"index": 0, "embedding": [1.0]}]}, "dimension"),
        ({"data": [{"index": 0, "embedding": [1.0, "bad"]}]}, "numeric"),
        ({"data": []}, "count"),
    ],
)
def test_embedding_response_validation_is_governed(
    tmp_path: Path,
    payload: dict[str, object],
    message: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        with pytest.raises(ValueError, match=message):
            make_embedder(settings, runtime=runtime).embed_one("first")
        assert runtime.governor.snapshot()["settled"]["requests"] == 1
    finally:
        runtime.close()
        http_client.close()


def test_embedding_transport_failure_is_normalized(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        with pytest.raises(ProviderCallError) as captured:
            make_embedder(settings, runtime=runtime).embed_one("first")
        assert (captured.value.category, captured.value.attempts) == ("upstream", 1)
    finally:
        runtime.close()
        http_client.close()


def test_embedding_transport_preserves_connect_and_read_timeouts(tmp_path: Path) -> None:
    observed: dict[str, float] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        observed.update(request.extensions["timeout"])
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
        )

    settings = _settings(
        tmp_path,
        embedding_connect_timeout=1.25,
        embedding_read_timeout=9.5,
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        make_embedder(settings, runtime=runtime).embed_one("first")
        assert observed["connect"] == 1.25
        assert observed["read"] == 9.5
    finally:
        runtime.close()
        http_client.close()


def test_token_priced_embedding_reservation_fails_closed_before_network(tmp_path: Path) -> None:
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, json={})

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="embedding",
        provider="dashscope",
        model="text-embedding-v4",
        million_input_tokens=1,
    )
    settings = _settings(
        tmp_path,
        usage_price_book_path=str(price_book),
        usage_daily_cost_limit_microunits=1,
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        with pytest.raises(UsageLimitExceededError, match="cost"):
            make_embedder(settings, runtime=runtime).embed_one("first")
        assert not called
    finally:
        runtime.close()
        http_client.close()


def test_token_priced_embedding_missing_usage_settles_unknown_cost(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
        )

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="embedding",
        provider="dashscope",
        model="text-embedding-v4",
        million_input_tokens=1,
    )
    settings = _settings(tmp_path, usage_price_book_path=str(price_book))
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        make_embedder(settings, runtime=runtime).embed_one("first")
        snapshot = runtime.governor.snapshot()
        assert snapshot["settled"]["cost_microunits"] is None
        assert snapshot["unknown_cost_count"] == 1
    finally:
        runtime.close()
        http_client.close()
