import httpx

from hl_mem.llm.providers import DashScopeProvider, OpenAICompatibleProvider, ZhipuProvider
from hl_mem.llm.types import LLMMessage, LLMRequest, StructuredOutputMode, StructuredOutputSpec


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role="user", content="extract")],
        structured_output=StructuredOutputSpec(
            name="extraction_response",
            schema={"type": "object", "additionalProperties": False},
            preferred_mode=StructuredOutputMode.JSON_SCHEMA,
        ),
    )


def test_openai_compatible_provider_builds_strict_schema_payload() -> None:
    payload = OpenAICompatibleProvider().build_payload(
        "model",
        _request(),
        StructuredOutputMode.JSON_SCHEMA,
    )
    assert payload["response_format"]["json_schema"]["strict"] is True


def test_dashscope_and_zhipu_default_to_json_object_capability() -> None:
    assert DashScopeProvider().capabilities.json_schema_strict is False
    assert ZhipuProvider().capabilities.json_schema_strict is False


def test_provider_parses_response_metadata() -> None:
    response = OpenAICompatibleProvider().parse_response(
        {
            "id": "request-1",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 7},
        }
    )
    assert (response.finish_reason, response.usage_total_tokens, response.raw_request_id) == (
        "stop",
        7,
        "request-1",
    )


def test_only_explicit_structured_format_errors_are_unsupported() -> None:
    provider = OpenAICompatibleProvider()
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(400, request=request, text="response_format json_schema unsupported")
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    assert provider.is_structured_mode_unsupported(error) is True
