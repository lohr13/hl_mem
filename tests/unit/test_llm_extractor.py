import json
import logging

import httpx
import pytest

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import SYSTEM_PROMPT, LLMExtractor
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import ZhipuProvider
from hl_mem.llm.types import LLMRequest, LLMResponse


class _FakeLLMClient:
    """测试用 LLMClient 替身，返回预设响应。"""

    class _Provider:
        """最小 provider 标识。"""

        name = "fake"

    provider = _Provider()
    model = "test-model"

    def __init__(self, response_content: str, usage_tokens: int = 12) -> None:
        self._content = response_content
        self._tokens = usage_tokens
        self.last_request: LLMRequest | None = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        """记录请求并返回预设响应。"""
        self.last_request = request
        return LLMResponse(self._content, "stop", self._tokens)


def test_parses_fenced_json_and_normalizes_entity() -> None:
    raw = """```json
    {"claims":[{"subject":"用户","predicate":"使用","value":"PG","qualifiers":{},
    "confidence":0.9,"volatility":"stable","reason":"明确陈述"}],"should_memorize":true}
    ```"""
    client = _FakeLLMClient(raw)
    extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))
    claims = extractor.extract({"text": "数据库使用 PG"})
    assert claims[0].value == "PostgreSQL"
    assert extractor.last_usage_tokens == 12


def test_should_memorize_false_returns_no_claims() -> None:
    client = _FakeLLMClient('{"claims":[],"should_memorize":false}')
    assert LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract("闲聊") == []


def test_extract_emits_json_debug_metrics(caplog) -> None:
    """提取摘要日志应包含定位覆盖缺口和成本所需的结构化指标。"""
    raw = (
        '{"claims":[{"subject":"用户","predicate":"使用","value":"CUDA","qualifiers":{},'
        '"reason":"用户明确说明 GPU 工具链"}],"should_memorize":true}'
    )
    client = _FakeLLMClient(raw, usage_tokens=12)
    extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))

    with caplog.at_level(logging.DEBUG, logger="hl_mem.ingest.llm_extractor"):
        claims = extractor.extract(
            {"text": "用户使用 CUDA"},
            {"actor": "user", "session_id": "session-1"},
        )

    payload = json.loads(caplog.records[-1].message)
    assert len(claims) == 1
    assert payload == {
        "event": "llm_extraction",
        "actor": "user",
        "session_id": "session-1",
        "content_length": 9,
        "should_memorize": True,
        "reason": "用户明确说明 GPU 工具链",
        "claims_count": 1,
        "schema_retry_count": 0,
        "repair_count": 0,
        "llm_call_count": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 12,
    }


def test_extract_metrics_count_schema_retries_and_repairs(caplog) -> None:
    """日志计数应覆盖 schema 重试以及确定性 JSON 修复。"""

    class _SequenceClient(_FakeLLMClient):
        def __init__(self) -> None:
            super().__init__("")
            self.responses = iter(
                [
                    LLMResponse('{"claims":"invalid","should_memorize":true}', "stop", 3),
                    LLMResponse(
                        '{"claims":[{"subject":"用户","predicate":"使用","value":"CUDA","qualifiers":{},'
                        '"topic_tags":["硬件"]}],"should_memorize":true,"sensitivity":"普通"}',
                        "stop",
                        5,
                    ),
                ]
            )

        def complete(self, request: LLMRequest) -> LLMResponse:
            self.last_request = request
            return next(self.responses)

    extractor = LLMExtractor(_SequenceClient(), ChunkingPolicy(10_000, 0, 2), schema_retries=1)
    with caplog.at_level(logging.DEBUG, logger="hl_mem.ingest.llm_extractor"):
        extractor.extract("用户使用 CUDA", {"actor_type": "user", "session_id": "session-2"})

    payload = json.loads(caplog.records[-1].message)
    assert payload["schema_retry_count"] == 1
    assert payload["repair_count"] == 2
    assert payload["llm_call_count"] == 2
    assert payload["total_tokens"] == 8


def test_occurred_at_is_injected_into_user_prompt() -> None:
    client = _FakeLLMClient('{"claims":[],"should_memorize":true}')
    extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))
    occurred_at = "2026-07-21T08:30:00+08:00"
    extractor.extract("明天交付", {"occurred_at": occurred_at})
    assert client.last_request is not None
    assert occurred_at in client.last_request.messages[1].content


def test_normalizes_predicate_and_preserves_chinese_value() -> None:
    raw = (
        '{"claims":[{"subject":"用户","predicate":"Prefers",'
        '"value":"深色模式","qualifiers":{}}],"should_memorize":true}'
    )
    client = _FakeLLMClient(raw)
    claim = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract("我喜欢深色模式")[0]
    assert claim.predicate == "偏好"
    assert claim.value == "深色模式"


def test_invalid_json_is_rejected() -> None:
    client = _FakeLLMClient("not json")
    with pytest.raises(ValueError, match="valid JSON"):
        LLMExtractor(client, ChunkingPolicy(10_000, 0, 2)).extract("内容")


def test_llm_client_has_configured_retry() -> None:
    client = LLMClient(
        "key",
        "https://example.test",
        "model",
        provider=ZhipuProvider(),
        timeout=httpx.Timeout(42.0),
        max_attempts=3,
    )
    assert client.max_attempts == 3


def test_timeout_reads_from_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    from hl_mem.settings import Settings

    settings = Settings.from_env()
    assert settings.llm_timeout == 60.0


def test_prompt_requires_canonical_attribute() -> None:
    assert "canonical_attribute" in SYSTEM_PROMPT
    assert "preference.ui_theme" in SYSTEM_PROMPT


def test_claim_validates_canonical_attribute_against_predicate() -> None:
    valid = LLMExtractor._claim(
        {"predicate": "偏好", "value": "Codex", "canonical_attribute": "preference.tool_choice"}
    )
    invalid = LLMExtractor._claim({"predicate": "偏好", "value": "深色", "canonical_attribute": "invented.slot"})
    wrong_domain = LLMExtractor._claim({"predicate": "偏好", "value": "深色", "canonical_attribute": "config.port"})
    assert valid.canonical_attribute == "preference.tool_choice"
    # reconcile infers a valid attribute from "深色" content instead of returning custom.unknown
    assert invalid.canonical_attribute == "preference.ui_theme"
    # reconcile overrides wrong-domain attribute with content-inferred preference.ui_theme
    assert wrong_domain.canonical_attribute == "preference.ui_theme"
