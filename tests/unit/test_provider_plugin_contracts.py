from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from hl_mem.errors import ProviderCallError
from hl_mem.llm.types import LLMCapabilities
from hl_mem.plugins.contracts import (
    PROVIDER_API_VERSION,
    PROVIDER_ENTRY_POINT_GROUP,
    EmbeddingInvocation,
    ProviderCapability,
    ProviderEndpoint,
    ProviderFactoryContext,
    ProviderKey,
    ProviderRequest,
    ProviderResponse,
    ProviderStability,
    RerankInvocation,
    ValidatedImageInput,
)


def test_public_constants_and_capability_stability_are_unambiguous() -> None:
    assert PROVIDER_API_VERSION == 1
    assert PROVIDER_ENTRY_POINT_GROUP == "hl_mem.providers"
    assert [item.value for item in ProviderCapability] == [
        "llm",
        "embedding",
        "reranker",
        "image_describer",
    ]
    assert [item.value for item in ProviderStability] == ["stable", "experimental"]


def test_provider_request_repr_and_error_never_expose_sensitive_payloads() -> None:
    request = ProviderRequest(
        method="POST",
        url="https://provider.example.test/v1/run",
        headers={"Authorization": "Bearer top-secret"},
        json_body={"prompt": "private memory"},
        timeout_seconds=5.0,
    )

    rendered = repr(request)
    assert "top-secret" not in rendered
    assert "private memory" not in rendered
    assert request.method == "POST"


def test_provider_call_error_has_bounded_normalized_diagnostics() -> None:
    error = ProviderCallError(
        "rate_limit",
        "x" * 600,
        attempts=2,
        sent=True,
        http_status=429,
        provider_code="quota_exceeded",
        request_id="request-1",
        response_body={"error": "safe diagnostic"},
    )

    assert len(str(error)) == 512
    assert error.attempts == 2
    assert error.sent is True
    assert error.http_status == 429
    assert error.provider_code == "quota_exceeded"
    assert error.request_id == "request-1"
    assert error.response_body == {"error": "safe diagnostic"}


def test_contract_mappings_are_defensive_immutable_copies() -> None:
    headers = {"X-Test": "one"}
    request = ProviderRequest("POST", "https://provider.example.test", headers, {"input": [1]}, 2.0)
    headers["X-Test"] = "changed"

    assert request.headers["X-Test"] == "one"
    with pytest.raises(TypeError):
        request.headers["X-Test"] = "two"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        request.timeout_seconds = 3.0  # type: ignore[misc]


def test_endpoint_and_validated_image_hide_credentials_and_bytes() -> None:
    endpoint = ProviderEndpoint("https://provider.example.test", "top-secret", "model", 5.0, 2)
    image = ValidatedImageInput(b"private-image-bytes", "image/png", "a" * 64)

    assert "top-secret" not in repr(endpoint)
    assert "private-image-bytes" not in repr(image)


def test_invocations_normalize_mutable_sequences_to_tuples() -> None:
    texts = ["one", "two"]
    documents = ["first", "second"]
    embedding = EmbeddingInvocation(texts, 2, "compatible", None)
    rerank = RerankInvocation("query", documents, 1)
    texts.append("three")
    documents.append("third")

    assert embedding.texts == ("one", "two")
    assert rerank.documents == ("first", "second")


def test_provider_identity_types_reject_invalid_or_ambiguous_values() -> None:
    with pytest.raises(ValueError, match="provider name"):
        ProviderKey(ProviderCapability.LLM, "Bad Provider")
    with pytest.raises(ValueError, match="timeout"):
        ProviderEndpoint("https://provider.example.test", "key", "model", 0.0, 1)
    with pytest.raises(ValueError, match="max_attempts"):
        ProviderEndpoint("https://provider.example.test", "key", "model", 1.0, 0)
    with pytest.raises(ValueError, match="attempts"):
        ProviderResponse(200, {}, {}, 0, None)


def test_factory_context_never_aliases_mutable_plugin_options() -> None:
    plugin_options = {"region": "cn", "nested": {"mode": "strict"}}
    context = ProviderFactoryContext(
        ProviderKey(ProviderCapability.LLM, "vendor"),
        {"max_tokens": 512},
        plugin_options,
    )
    plugin_options["region"] = "changed"

    assert context.plugin_options["region"] == "cn"
    with pytest.raises(TypeError):
        context.plugin_options["region"] = "us"  # type: ignore[index]


def test_llm_capabilities_remain_transport_neutral() -> None:
    capabilities = LLMCapabilities(json_object=True, json_schema_strict=False)
    assert capabilities.json_object is True
    assert capabilities.json_schema_strict is False
