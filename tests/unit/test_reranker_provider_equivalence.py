from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hl_mem.components import create_provider_runtime, make_reranker
from hl_mem.errors import ProviderCallError, UsageLimitExceededError
from hl_mem.settings import Settings
from tests.unit._usage_pricing_fixture import write_usage_price_book

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "providers" / "reranker_dashscope.json"


def _settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.for_test(),
        database_path=str(tmp_path / "memory.db"),
        reranker_mode="real",
        reranker_provider="dashscope",
        reranker_api_key="test-key",
        reranker_base_url="https://example.test",
        reranker_model="qwen3-rerank",
    )


def test_registry_reranker_matches_frozen_request_and_response(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    captured: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "json": json.loads(request.content)})
        return httpx.Response(200, request=request, json=fixture["response"])

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        assert reranker.rerank("query", ["first", "second"], 2) == [(1, 0.9), (0, 0.4)]
        assert captured == [{"url": fixture["url"], "json": fixture["request"]}]
        settled = runtime.governor.snapshot()["settled"]
        assert settled["requests"] == 1
        assert settled["rerank_documents"] == 2
        assert settled["input_tokens"] == 5
        assert settled["output_tokens"] == 1
    finally:
        runtime.close()
        http_client.close()


def test_empty_rerank_records_no_usage(tmp_path: Path) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"empty rerank sent {request.url}")

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(unexpected))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        assert reranker.rerank("query", []) == []
        assert reranker.last_outcome == "empty"
        assert runtime.governor.snapshot()["settled"]["requests"] == 0
    finally:
        runtime.close()
        http_client.close()


@pytest.mark.parametrize(
    "payload, error_class",
    [
        ({"output": {"results": [{"index": 99, "relevance_score": 1.0}]}}, "InvalidResultIndex"),
        ({"output": {"results": [{"index": 0, "relevance_score": "bad"}]}}, "InvalidRerankResponse"),
        ({"output": {}}, "InvalidRerankResponse"),
    ],
)
def test_invalid_rerank_result_is_contained_and_governed(
    tmp_path: Path,
    payload: dict[str, object],
    error_class: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        assert reranker.rerank("query", ["only"], 1) == []
        assert reranker.last_outcome == "error"
        assert reranker.last_error_class == error_class
        assert runtime.governor.snapshot()["settled"]["rerank_documents"] == 1
    finally:
        runtime.close()
        http_client.close()


def test_reranker_retry_counts_each_actual_attempt(tmp_path: Path) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            request=request,
            json={"output": {"results": [{"index": 0, "relevance_score": 0.8}]}},
        )

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        assert reranker.rerank("query", ["only"], 1) == [(0, 0.8)]
        settled = runtime.governor.snapshot()["settled"]
        assert attempts == 2
        assert settled["requests"] == 2
        assert settled["rerank_documents"] == 2
    finally:
        runtime.close()
        http_client.close()


def test_reranker_auth_failure_remains_normalized_for_recall_fallback(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request, json={"error": {"code": "invalid_key"}})

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        with pytest.raises(ProviderCallError) as captured:
            reranker.rerank("query", ["only"], 1)
        assert (captured.value.category, reranker.last_outcome) == ("auth", "error")
    finally:
        runtime.close()
        http_client.close()


def test_token_priced_reranker_reservation_fails_closed_before_network(tmp_path: Path) -> None:
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, json={})

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="reranker",
        provider="dashscope",
        model="qwen3-rerank",
        million_input_tokens=1,
    )
    settings = replace(
        _settings(tmp_path),
        usage_price_book_path=str(price_book),
        usage_daily_cost_limit_microunits=1,
    )
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        with pytest.raises(UsageLimitExceededError, match="cost"):
            reranker.rerank("query", ["document"], 1)
        assert not called
    finally:
        runtime.close()
        http_client.close()


def test_token_priced_reranker_missing_usage_settles_unknown_cost(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"output": {"results": [{"index": 0, "relevance_score": 0.8}]}},
        )

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="reranker",
        provider="dashscope",
        model="qwen3-rerank",
        million_input_tokens=1,
    )
    settings = replace(_settings(tmp_path), usage_price_book_path=str(price_book))
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        reranker = make_reranker(settings, runtime=runtime)
        assert reranker is not None
        assert reranker.rerank("query", ["document"], 1) == [(0, 0.8)]
        snapshot = runtime.governor.snapshot()
        assert snapshot["settled"]["cost_microunits"] is None
        assert snapshot["unknown_cost_count"] == 1
    finally:
        runtime.close()
        http_client.close()
