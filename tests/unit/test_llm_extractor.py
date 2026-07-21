import httpx
import pytest

from hl_mem.ingest.llm_extractor import LLMExtractor


class Response:
    def __init__(self, content: str, tokens: int = 12) -> None:
        self.content = content
        self.tokens = tokens

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": self.content}}],
            "usage": {"total_tokens": self.tokens},
        }


def test_parses_fenced_json_and_normalizes_entity(monkeypatch) -> None:
    raw = """```json
    {"claims":[{"subject":"用户","predicate":"使用","value":"PG","qualifiers":{},
    "confidence":0.9,"volatility":"stable","reason":"明确陈述"}],"should_memorize":true}
    ```"""
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response(raw))
    extractor = LLMExtractor("key", "https://example.test/v1", "model")
    claims = extractor.extract({"text": "数据库使用 PG"})
    assert claims[0].value == "PostgreSQL"
    assert extractor.last_usage_tokens == 12


def test_should_memorize_false_returns_no_claims(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: Response('{"claims":[],"should_memorize":false}')
    )
    assert LLMExtractor("key", "https://example.test", "model").extract("闲聊") == []


def test_occurred_at_is_injected_into_user_prompt(monkeypatch) -> None:
    captured = {}

    def post(*args, **kwargs):
        captured.update(kwargs["json"])
        return Response('{"claims":[],"should_memorize":true}')

    monkeypatch.setattr(httpx, "post", post)
    occurred_at = "2026-07-21T08:30:00+08:00"
    LLMExtractor("key", "https://example.test", "model").extract(
        "明天交付", {"occurred_at": occurred_at}
    )
    assert occurred_at in captured["messages"][1]["content"]


def test_normalizes_predicate_and_preserves_chinese_value(monkeypatch) -> None:
    raw = ('{"claims":[{"subject":"用户","predicate":"Prefers",'
           '"value":"深色模式","qualifiers":{}}],"should_memorize":true}')
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response(raw))
    claim = LLMExtractor("key", "https://example.test", "model").extract("我喜欢深色模式")[0]
    assert claim.predicate == "偏好"
    assert claim.value == "深色模式"


def test_invalid_json_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response("not json"))
    with pytest.raises(ValueError, match="valid JSON"):
        LLMExtractor("key", "https://example.test", "model").extract("内容")


def test_http_call_has_timeout_and_two_retries(monkeypatch) -> None:
    calls: list[float] = []

    def fail(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", fail)
    monkeypatch.setattr("hl_mem.ingest.llm_extractor.time.sleep", lambda _: None)
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    with pytest.raises(httpx.ConnectError):
        LLMExtractor("key", "https://example.test", "model", timeout=42.0).extract("内容")
    assert calls == [42.0, 42.0, 42.0]


def test_timeout_reads_from_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    ext = LLMExtractor("key", "https://example.test", "model")
    assert ext.timeout == 60.0
