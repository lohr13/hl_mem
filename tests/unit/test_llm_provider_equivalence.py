from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from hl_mem.components import create_provider_runtime, make_llm_client
from hl_mem.errors import UsageLimitExceededError
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.settings import Settings

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "providers" / "llm_openai_compatible.json"


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role="user", content="extract")],
        structured_output=StructuredOutputSpec(
            name="extraction_response",
            schema={"type": "object", "additionalProperties": False},
            preferred_mode=StructuredOutputMode.JSON_SCHEMA,
        ),
    )


def _settings(tmp_path: Path, provider: str = "openai_compatible", **changes: object) -> Settings:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
        llm_api_key="test-key",
        llm_base_url="https://example.test/v1",
        llm_model="test-model",
        llm_provider=provider,
        llm_max_attempts=1,
        embedder_mode="fake",
        reranker_mode="off",
        query_expansion_mode="off",
        relation_discovery_mode="off",
        image_describer_mode="off",
    )
    return replace(settings, **changes)


@pytest.mark.parametrize("provider", ("dashscope", "zhipu", "openai_compatible"))
def test_registry_llm_matches_frozen_request_and_response(tmp_path: Path, provider: str) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    captured: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, request=request, json=fixture["response"])

    settings = _settings(tmp_path, provider)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        response = make_llm_client(settings, runtime=runtime).complete(_request())
        expected = dict(fixture["request"])
        expected["model"] = "test-model"
        if provider == "dashscope":
            expected["enable_thinking"] = False
            expected["response_format"] = {"type": "json_object"}
        elif provider == "zhipu":
            expected["response_format"] = {"type": "json_object"}
        assert captured == [expected]
        assert asdict(response) == fixture["normalized_response"]
        assert runtime.governor.snapshot()["settled"]["requests"] == 1
    finally:
        runtime.close()
        http_client.close()


def test_structured_fallback_is_two_governed_calls(tmp_path: Path) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(400, request=request, json={"error": {"message": "json_schema unsupported"}})
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 3}},
        )

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        assert make_llm_client(settings, runtime=runtime).complete(_request()).content == "{}"
        assert attempts == 2
        assert runtime.governor.snapshot()["settled"]["requests"] == 2
    finally:
        runtime.close()
        http_client.close()


def test_missing_usage_is_settled_with_conservative_estimate(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})

    settings = _settings(tmp_path)
    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=http_client)
    try:
        make_llm_client(settings, runtime=runtime).complete(_request())
        settled = runtime.governor.snapshot()["settled"]
        assert settled["requests"] == 1
        assert settled["total_tokens"] > 0
        with sqlite3.connect(tmp_path / "memory.budget.db") as connection:
            assert connection.execute("SELECT status FROM usage_events").fetchone()[0] == "estimated"
    finally:
        runtime.close()
        http_client.close()


def test_fake_component_path_does_not_create_usage_sidecar(tmp_path: Path) -> None:
    from hl_mem.components import make_extractor

    settings = replace(Settings.for_test(), database_path=str(tmp_path / "memory.db"))

    make_extractor(settings)

    runtime = create_provider_runtime(settings, create_usage=False)
    runtime.close()

    assert not (tmp_path / "memory.budget.db").exists()


def test_two_llm_clients_cannot_overspend_one_request_budget(tmp_path: Path) -> None:
    request_started = threading.Event()
    release_request = threading.Event()
    network_calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        request_started.set()
        assert release_request.wait(5)
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": "{}"}}]})

    settings = _settings(tmp_path, usage_daily_request_limit=1)
    http_clients = [httpx.Client(transport=httpx.MockTransport(handle)) for _ in range(2)]
    runtimes = [create_provider_runtime(settings, client=item) for item in http_clients]
    clients = [make_llm_client(settings, runtime=item) for item in runtimes]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(clients[0].complete, _request())
            assert request_started.wait(5)
            with pytest.raises(UsageLimitExceededError, match="request limit"):
                clients[1].complete(_request())
            release_request.set()
            assert first.result().content == "{}"
        assert network_calls == 1
    finally:
        release_request.set()
        for runtime in runtimes:
            runtime.close()
        for http_client in http_clients:
            http_client.close()
