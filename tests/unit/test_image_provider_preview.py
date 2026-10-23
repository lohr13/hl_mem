from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hl_mem.components import create_provider_runtime, make_image_describer
from hl_mem.domain.content import ImagePart
from hl_mem.errors import UsageLimitExceededError
from hl_mem.plugins.contracts import (
    ImageProviderResult,
    ProviderCapability,
    ProviderEndpoint,
    ProviderRequest,
    ProviderResponse,
    ValidatedImageInput,
)
from hl_mem.security.image_input import ImageInputGuard
from hl_mem.settings import Settings
from tests.unit._usage_pricing_fixture import write_usage_price_book

PNG = b"\x89PNG\r\n\x1a\n" + b"safe-image-bytes"
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "providers" / "image_dashscope.json"


def _settings(tmp_path: Path) -> Settings:
    return replace(
        Settings.for_test(),
        database_path=str(tmp_path / "memory.db"),
        image_describer_mode="on",
        image_describer_provider="dashscope",
        image_describer_api_key="test-key",
        image_describer_base_url="https://example.test/v1",
        image_describer_model="qwen-vl",
        image_max_bytes=64,
    )


def _image() -> ImagePart:
    return ImagePart(None, base64.b64encode(PNG).decode(), "image/png", page=2, region=(0.1, 0.2, 0.8, 0.9))


def test_registry_image_provider_matches_frozen_request_and_response(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    captured: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "json": json.loads(request.content)})
        return httpx.Response(200, request=request, json=fixture["response"])

    settings = _settings(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=client)
    try:
        describer = make_image_describer(settings, runtime=runtime)
        assert describer is not None
        result = describer.describe(_image(), timeout_seconds=7.0)
        assert (result.caption, result.ocr_text, result.model, result.confidence) == (
            "a receipt",
            "TOTAL 12.00",
            "qwen-vl",
            0.8,
        )
        assert (result.locator.uri, result.locator.media_type, result.locator.page) == (None, "image/png", 2)
        assert result.locator.sha256
        assert captured == [{"url": fixture["url"], "json": fixture["request"]}]
        settled = runtime.governor.snapshot()["settled"]
        assert (settled["requests"], settled["images"], settled["input_tokens"], settled["output_tokens"]) == (
            1,
            1,
            7,
            4,
        )
    finally:
        runtime.close()
        client.close()


def test_plugin_adapter_receives_only_validated_bytes(tmp_path: Path) -> None:
    seen: list[ValidatedImageInput] = []

    class RecordingProvider:
        def build_request(self, endpoint: ProviderEndpoint, image: ValidatedImageInput) -> ProviderRequest:
            seen.append(image)
            return ProviderRequest("POST", endpoint.base_url, {}, {"ok": True}, endpoint.timeout_seconds)

        def parse_response(self, response: ProviderResponse) -> ImageProviderResult:
            return ImageProviderResult("caption", None, "external", None)

    settings = _settings(tmp_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request, json={"ok": True}))
    )
    runtime = create_provider_runtime(settings, client=client)
    try:
        from hl_mem.ingest.image_describer import GovernedImageDescriber

        describer = GovernedImageDescriber(
            endpoint=ProviderEndpoint("https://provider.test", "key", "model", 5.0, 1),
            provider=RecordingProvider(),
            governed=runtime.governed_call(
                capability=ProviderCapability.IMAGE_DESCRIBER,
                provider="dashscope",
                operation="describe",
                model="model",
            ),
            input_guard=ImageInputGuard(64, False, ()),
        )
        result = describer.describe(_image(), timeout_seconds=5.0)
        assert result.ocr_text == ""
        assert seen and seen[0].data == PNG
        assert not hasattr(seen[0], "uri")
    finally:
        runtime.close()
        client.close()


def test_guard_rejection_creates_no_provider_usage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    called = False

    def provider_call(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, json={})

    client = httpx.Client(transport=httpx.MockTransport(provider_call))
    runtime = create_provider_runtime(settings, client=client)
    try:
        describer = make_image_describer(settings, runtime=runtime)
        assert describer is not None
        with pytest.raises(ValueError, match="public"):
            describer.describe(ImagePart("https://127.0.0.1/a.png", None, "image/png"), timeout_seconds=5.0)
        assert not called
        assert runtime.governor.snapshot()["settled"]["requests"] == 0
    finally:
        runtime.close()
        client.close()


def test_token_priced_image_reservation_fails_closed_before_network(tmp_path: Path) -> None:
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, request=request, json={})

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="image_describer",
        provider="dashscope",
        model="qwen-vl",
        million_input_tokens=1,
    )
    settings = replace(
        _settings(tmp_path),
        usage_price_book_path=str(price_book),
        usage_daily_cost_limit_microunits=1,
    )
    client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=client)
    try:
        describer = make_image_describer(settings, runtime=runtime)
        assert describer is not None
        with pytest.raises(UsageLimitExceededError, match="cost"):
            describer.describe(_image(), timeout_seconds=5.0)
        assert not called
    finally:
        runtime.close()
        client.close()


def test_token_priced_image_missing_usage_settles_unknown_cost(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "model": "qwen-vl",
                "choices": [{"message": {"content": '{"caption":"receipt","confidence":null}'}}],
            },
        )

    price_book = write_usage_price_book(
        tmp_path / "prices.json",
        capability="image_describer",
        provider="dashscope",
        model="qwen-vl",
        million_input_tokens=1,
    )
    settings = replace(_settings(tmp_path), usage_price_book_path=str(price_book))
    client = httpx.Client(transport=httpx.MockTransport(handle))
    runtime = create_provider_runtime(settings, client=client)
    try:
        describer = make_image_describer(settings, runtime=runtime)
        assert describer is not None
        describer.describe(_image(), timeout_seconds=5.0)
        snapshot = runtime.governor.snapshot()
        assert snapshot["settled"]["cost_microunits"] is None
        assert snapshot["unknown_cost_count"] == 1
    finally:
        runtime.close()
        client.close()
