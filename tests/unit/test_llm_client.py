import httpx
import pytest

from hl_mem.llm.client import LLMClient
from hl_mem.observability.llm_spans import LLMSpanRecorder
from hl_mem.llm.providers import DashScopeProvider, OpenAICompatibleProvider
from hl_mem.llm.types import LLMMessage, LLMRequest, StructuredOutputMode, StructuredOutputSpec
from hl_mem.storage.database import Database


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


def test_client_records_success_span(tmp_path, monkeypatch) -> None:
    """LLMClient 成功返回时应写入调用账本。"""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: Response(
            {
                "id": "request-1",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        ),
    )
    connection = Database(tmp_path / "client-span.db").open()
    client = LLMClient(
        "key",
        "https://example.test/v1",
        "model",
        OpenAICompatibleProvider(),
        httpx.Timeout(10),
        1,
        span_recorder=LLMSpanRecorder(connection),
    )

    client.complete(_request())

    row = connection.execute("SELECT * FROM llm_call_spans").fetchone()
    assert (row["status"], row["raw_request_id"], row["total_tokens"]) == ("success", "request-1", 3)


def test_client_records_error_span(tmp_path, monkeypatch) -> None:
    """LLMClient 抛出异常前应写入失败 span。"""

    def fail(*args, **kwargs):
        raise RuntimeError("network failed")

    monkeypatch.setattr(httpx, "post", fail)
    connection = Database(tmp_path / "client-error-span.db").open()
    client = LLMClient(
        "key",
        "https://example.test/v1",
        "model",
        OpenAICompatibleProvider(),
        httpx.Timeout(10),
        1,
        span_recorder=LLMSpanRecorder(connection),
    )

    with pytest.raises(RuntimeError, match="network failed"):
        client.complete(_request())

    row = connection.execute("SELECT status,error_class FROM llm_call_spans").fetchone()
    assert tuple(row) == ("error", "RuntimeError")
