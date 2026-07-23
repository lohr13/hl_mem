import httpx

from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import DashScopeProvider, OpenAICompatibleProvider
from hl_mem.llm.types import LLMMessage, LLMRequest, StructuredOutputMode, StructuredOutputSpec


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role="user", content="extract")],
        structured_output=StructuredOutputSpec(
            name="extraction_response",
            schema={"type": "object"},
            preferred_mode=StructuredOutputMode.JSON_SCHEMA,
        ),
    )


def test_dashscope_uses_global_post_and_json_object(monkeypatch) -> None:
    captured: dict = {}

    def post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response(
            {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 3},
            }
        )

    monkeypatch.setattr(httpx, "post", post)
    client = LLMClient(
        "key",
        "https://example.test/v1",
        "model",
        DashScopeProvider(),
        httpx.Timeout(10),
        3,
    )
    assert client.complete(_request()).usage_total_tokens == 3
    assert captured["response_format"] == {"type": "json_object"}


def test_strict_capable_provider_receives_json_schema(monkeypatch) -> None:
    captured: dict = {}

    def post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(httpx, "post", post)
    client = LLMClient(
        "key",
        "https://example.test/v1",
        "model",
        OpenAICompatibleProvider(),
        httpx.Timeout(10),
        3,
    )
    client.complete(_request())
    assert captured["response_format"]["type"] == "json_schema"
