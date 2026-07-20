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
    with pytest.raises(httpx.ConnectError):
        LLMExtractor("key", "https://example.test", "model").extract("内容")
    assert calls == [30.0, 30.0, 30.0]
