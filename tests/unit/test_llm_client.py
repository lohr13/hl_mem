from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

import httpx
import pytest

from hl_mem.components import create_provider_runtime, make_llm_client
from hl_mem.errors import ProviderCallError, UsageLimitExceededError
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.observability.llm_spans import LLMSpanRecorder
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from tests.unit._usage_pricing_fixture import write_usage_price_book


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role="user", content="extract")],
        structured_output=StructuredOutputSpec(
            name="extraction_response",
            schema={"type": "object"},
            preferred_mode=StructuredOutputMode.JSON_SCHEMA,
        ),
    )


def _client(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    provider: str,
    span_recorder: LLMSpanRecorder | None = None,
    **changes: object,
):
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
        llm_api_key="key",
        llm_base_url="https://example.test/v1",
        llm_model="model",
        llm_provider=provider,
        llm_max_attempts=1,
        embedder_mode="fake",
        reranker_mode="off",
        query_expansion_mode="off",
        relation_discovery_mode="off",
        image_describer_mode="off",
    )
    settings = replace(settings, **changes)
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    runtime = create_provider_runtime(settings, client=http_client)
    client = make_llm_client(settings, runtime=runtime, span_recorder=span_recorder)
    return client, runtime, http_client


def test_dashscope_uses_governed_transport_and_json_object(tmp_path: Path) -> None:
    captured: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 3},
            },
        )

    client, runtime, http_client = _client(tmp_path, handle, provider="dashscope")
    try:
        assert client.complete(_request()).usage_total_tokens == 3
        assert captured["response_format"] == {"type": "json_object"}
        assert runtime.governor.snapshot()["settled"]["requests"] == 1
    finally:
        runtime.close()
        http_client.close()


def test_strict_capable_provider_receives_json_schema(tmp_path: Path) -> None:
    captured: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})

    client, runtime, http_client = _client(tmp_path, handle, provider="openai_compatible")
    try:
        client.complete(_request())
        assert captured["response_format"]["type"] == "json_schema"
    finally:
        runtime.close()
        http_client.close()


def test_chat_template_thinking_control_selects_json_object(tmp_path: Path) -> None:
    captured: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})

    client, runtime, http_client = _client(
        tmp_path,
        handle,
        provider="openai_compatible",
        llm_thinking_control="chat_template_kwargs",
    )
    try:
        client.complete(_request())
        assert captured["response_format"] == {"type": "json_object"}
    finally:
        runtime.close()
        http_client.close()


def test_client_records_success_span(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "request-1",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    connection = Database(tmp_path / "client-span.db").open()
    client, runtime, http_client = _client(
        tmp_path,
        handle,
        provider="openai_compatible",
        span_recorder=LLMSpanRecorder(connection),
    )
    try:
        client.complete(_request())
        row = connection.execute("SELECT * FROM llm_call_spans").fetchone()
        assert (row["status"], row["raw_request_id"], row["total_tokens"]) == (
            "success",
            "request-1",
            3,
        )
    finally:
        runtime.close()
        http_client.close()
        connection.close()


def test_client_records_normalized_error_span(tmp_path: Path) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network failed", request=request)

    connection = Database(tmp_path / "client-error-span.db").open()
    client, runtime, http_client = _client(
        tmp_path,
        fail,
        provider="openai_compatible",
        span_recorder=LLMSpanRecorder(connection),
    )
    try:
        with pytest.raises(ProviderCallError, match="Provider HTTP call failed"):
            client.complete(_request())
        row = connection.execute("SELECT status,error_class FROM llm_call_spans").fetchone()
        assert tuple(row) == ("error", "upstream")
    finally:
        runtime.close()
        http_client.close()
        connection.close()


def test_token_priced_llm_reservation_fails_closed_before_network(tmp_path: Path) -> None:
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="llm",
        provider="openai_compatible",
        model="model",
        million_input_tokens=1,
    )
    client, runtime, http_client = _client(
        tmp_path,
        handle,
        provider="openai_compatible",
        usage_price_book_path=str(price_book),
        usage_daily_cost_limit_microunits=1,
    )
    try:
        with pytest.raises(UsageLimitExceededError, match="cost"):
            client.complete(_request())
        assert not called
    finally:
        runtime.close()
        http_client.close()


def test_token_priced_llm_missing_usage_settles_unknown_cost(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="llm",
        provider="openai_compatible",
        model="model",
        million_input_tokens=1,
    )
    client, runtime, http_client = _client(
        tmp_path,
        handle,
        provider="openai_compatible",
        usage_price_book_path=str(price_book),
    )
    try:
        client.complete(_request())
        snapshot = runtime.governor.snapshot()
        assert snapshot["settled"]["cost_microunits"] is None
        assert snapshot["unknown_cost_count"] == 1
    finally:
        runtime.close()
        http_client.close()
