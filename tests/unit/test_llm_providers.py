import httpx
import pytest

from hl_mem.components import make_llm_client
from hl_mem.llm.providers import (
    DashScopeProvider,
    OpenAICompatibleProvider,
    ZhipuProvider,
)
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.settings import Settings


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


def test_auto_thinking_control_preserves_provider_payloads() -> None:
    base_payload = {
        "model": "model",
        "messages": [{"role": "user", "content": "extract"}],
        "response_format": {"type": "json_object"},
    }

    assert (
        OpenAICompatibleProvider().build_payload(
            "model",
            _request(),
            StructuredOutputMode.JSON_OBJECT,
        )
        == base_payload
    )
    assert (
        ZhipuProvider().build_payload(
            "model",
            _request(),
            StructuredOutputMode.JSON_OBJECT,
        )
        == base_payload
    )
    assert DashScopeProvider().build_payload(
        "model",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    ) == {**base_payload, "enable_thinking": False}


def test_openai_compatible_provider_nests_chat_template_thinking_control() -> None:
    payload = OpenAICompatibleProvider(
        enable_thinking=False,
        thinking_control="chat_template_kwargs",
    ).build_payload(
        "model",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    )

    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "enable_thinking" not in payload


def test_openai_compatible_provider_omits_max_tokens_by_default() -> None:
    payload = OpenAICompatibleProvider().build_payload(
        "model",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    )

    assert "max_tokens" not in payload


def test_openai_compatible_provider_includes_configured_max_tokens() -> None:
    payload = OpenAICompatibleProvider(max_tokens=4000).build_payload(
        "model",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    )

    assert payload["max_tokens"] == 4000


def test_dashscope_and_zhipu_default_to_json_object_capability() -> None:
    assert DashScopeProvider().capabilities.json_schema_strict is False
    assert ZhipuProvider().capabilities.json_schema_strict is False


def test_dashscope_provider_disables_thinking_in_payload_by_default() -> None:
    payload = DashScopeProvider().build_payload(
        "qwen3.7-plus",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    )

    assert payload["enable_thinking"] is False
    assert "extra_body" not in payload


def test_dashscope_provider_can_enable_thinking_in_payload() -> None:
    payload = DashScopeProvider(enable_thinking=True, max_tokens=4000).build_payload(
        "qwen3.7-plus",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    )

    assert payload["enable_thinking"] is True
    assert payload["max_tokens"] == 4000
    assert "extra_body" not in payload


def test_make_llm_client_applies_max_tokens_to_zhipu_without_thinking_fields() -> None:
    client = make_llm_client(
        Settings(
            llm_api_key="test-key",
            llm_provider="zhipu",
            llm_max_tokens=4000,
        )
    )

    payload = client.provider.build_payload(
        "glm-5.3-flash",
        _request(),
        StructuredOutputMode.JSON_OBJECT,
    )

    assert payload["max_tokens"] == 4000
    assert "enable_thinking" not in payload
    assert "thinking" not in payload


def test_provider_parses_response_metadata() -> None:
    response = OpenAICompatibleProvider().parse_response(
        {
            "id": "request-1",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 7},
        }
    )
    assert (
        response.finish_reason,
        response.usage_total_tokens,
        response.raw_request_id,
    ) == (
        "stop",
        7,
        "request-1",
    )


def test_provider_parses_token_breakdown() -> None:
    """Provider 应从 usage 中提取输入、输出和缓存 token。"""
    response = OpenAICompatibleProvider().parse_response(
        {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        }
    )
    assert (response.input_tokens, response.output_tokens, response.cached_tokens) == (
        5,
        3,
        2,
    )


def test_chat_template_thinking_control_strips_empty_think_prefix_before_json() -> None:
    response = OpenAICompatibleProvider(
        thinking_control="chat_template_kwargs",
    ).parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": '<think>\n\n</think>\n  {"claims": []}',
                    }
                }
            ],
        }
    )

    assert response.content == '{"claims": []}'


@pytest.mark.parametrize(
    "content",
    [
        '<think>reasoning</think>\n{"claims": []}',
        "<think>\n</think>\nplain text",
        'prefix <think>\n</think>\n{"claims": []}',
    ],
)
def test_chat_template_thinking_control_preserves_unsafe_think_content(content: str) -> None:
    response = OpenAICompatibleProvider(
        thinking_control="chat_template_kwargs",
    ).parse_response(
        {"choices": [{"message": {"content": content}}]},
    )

    assert response.content == content


def test_auto_thinking_control_preserves_empty_think_prefix() -> None:
    content = '<think>\n</think>\n{"claims": []}'

    response = OpenAICompatibleProvider().parse_response(
        {"choices": [{"message": {"content": content}}]},
    )

    assert response.content == content


def test_only_explicit_structured_format_errors_are_unsupported() -> None:
    provider = OpenAICompatibleProvider()
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(400, request=request, text="response_format json_schema unsupported")
    error = httpx.HTTPStatusError("bad request", request=request, response=response)
    assert provider.is_structured_mode_unsupported(error) is True
