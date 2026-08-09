import json
import logging
import re

import httpx
import pytest

from hl_mem.domain.claims.attributes import PREDICATE_ATTRIBUTE_MAP
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import (
    ENGLISH_SYSTEM_PROMPT,
    LANGUAGE_ROUTER_VERSION,
    LLM_EXTRACTOR_VERSION,
    PROMPT_HASH,
    SYSTEM_PROMPT,
    LLMExtractor,
    compute_prompt_hash,
)
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import ZhipuProvider
from hl_mem.llm.types import LLMRequest, LLMResponse
from hl_mem.observability.audit import audit_scope


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


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def emit(self, phase, action, outcome, *, detail=None, **_dimensions):
        self.events.append((phase, action, outcome, detail or {}))
        return True


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


def test_timeout_is_configurable() -> None:
    from hl_mem.settings import Settings

    settings = Settings(llm_timeout=60.0)
    assert settings.llm_timeout == 60.0


def test_prompt_requires_compact_candidate_fields_only() -> None:
    for field in ("subject", "value", "kind", "confidence", "notability", "evidence_quote"):
        assert f'"{field}"' in SYSTEM_PROMPT
    assert "canonical_attribute" not in SYSTEM_PROMPT
    assert "topic_tags" not in SYSTEM_PROMPT


def test_prompt_extracts_bounded_assistant_durable_outputs() -> None:
    for signal in (
        "durable output",
        "table rows",
        "numbered list items",
        "script settings",
        "contact details",
        "tool-to-algorithm mappings",
        "Do not memorize the whole assistant answer",
    ):
        assert signal in ENGLISH_SYSTEM_PROMPT
    for signal in (
        "可再次引用的 durable output",
        "表格行",
        "编号列表项",
        "脚本设定",
        "联系人信息",
        "工具到算法的映射",
        "禁止记忆整段 assistant 回答",
    ):
        assert signal in SYSTEM_PROMPT


def test_prompt_hash_is_stable_and_has_expected_format() -> None:
    first = compute_prompt_hash(SYSTEM_PROMPT)
    second = compute_prompt_hash(SYSTEM_PROMPT)

    assert first == second == PROMPT_HASH
    assert re.fullmatch(r"[0-9a-f]{12}", PROMPT_HASH)


def test_prompt_hash_changes_when_prompt_changes() -> None:
    assert compute_prompt_hash(f"{SYSTEM_PROMPT}\nchanged") != PROMPT_HASH


def test_prompt_hash_changes_when_schema_or_postprocess_rules_change() -> None:
    assert compute_prompt_hash(SYSTEM_PROMPT, response_schema={"type": "object"}) != PROMPT_HASH
    assert compute_prompt_hash(SYSTEM_PROMPT, postprocess_rules={"revision": 2}) != PROMPT_HASH


def test_prompt_hash_includes_explicit_language_router_version() -> None:
    assert LANGUAGE_ROUTER_VERSION == "language-router-v1"
    assert (
        compute_prompt_hash(
            SYSTEM_PROMPT,
            language_router_version="language-router-v2",
        )
        != PROMPT_HASH
    )


def test_prompt_hash_includes_predicate_attribute_rules(monkeypatch) -> None:
    monkeypatch.setitem(PREDICATE_ATTRIBUTE_MAP, "偏好", (("preference.other",), "preference.other"))

    assert compute_prompt_hash(SYSTEM_PROMPT) != PROMPT_HASH


def test_llm_extractor_version_contains_prompt_hash() -> None:
    assert LLM_EXTRACTOR_VERSION == f"llm-v2+{PROMPT_HASH}"
    assert re.fullmatch(r"llm-v2\+[0-9a-f]{12}", LLM_EXTRACTOR_VERSION)


def test_claim_validates_canonical_attribute_against_predicate() -> None:
    valid = LLMExtractor._claim(
        {
            "predicate": "偏好",
            "value": "Codex",
            "canonical_attribute": "preference.tool_choice",
        }
    )
    invalid = LLMExtractor._claim({"predicate": "偏好", "value": "深色", "canonical_attribute": "invented.slot"})
    wrong_domain = LLMExtractor._claim({"predicate": "偏好", "value": "深色", "canonical_attribute": "config.port"})
    assert valid.canonical_attribute == "preference.tool_choice"
    # reconcile infers a valid attribute from "深色" content instead of returning custom.unknown
    assert invalid.canonical_attribute == "preference.ui_theme"
    # reconcile overrides wrong-domain attribute with content-inferred preference.ui_theme
    assert wrong_domain.canonical_attribute == "preference.ui_theme"


def test_secret_claims_are_rejected_without_copying_values_into_audit() -> None:
    secret_values = [
        "GitHub recovery codes 是 abcde-fghij",
        "服务令牌是 sk-AbC123456789xyz",
        "数据库 password=hunter2",
        "部署配置 api_key=plain-text-key",
        "内部令牌为 Abcdef1234567890",
    ]
    raw = json.dumps(
        {
            "claims": [
                *[
                    {
                        "predicate": "事实",
                        "value": value,
                        "confidence": 0.9,
                    }
                    for value in secret_values
                ],
                {
                    "predicate": "配置",
                    "value": "hl_mem 使用 SQLite WAL 模式",
                    "confidence": 0.9,
                },
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )
    audit = _RecordingAudit()
    source_text = "\n".join([*secret_values, "hl_mem 使用 SQLite WAL 模式"])

    with audit_scope(audit):
        claims = LLMExtractor(
            _FakeLLMClient(raw),
            ChunkingPolicy(10_000, 0, 2),
        ).extract(source_text)

    assert [claim.value for claim in claims] == ["hl_mem 使用 SQLite WAL 模式"]
    rejected = [event for event in audit.events if event[1] == "secret_rejected"]
    assert len(rejected) == 1
    assert rejected[0][:3] == ("extract", "secret_rejected", "rejected")
    assert rejected[0][3]["count"] == len(secret_values)
    assert rejected[0][3]["reason_counts"] == {
        "mixed_alnum_token": 1,
        "recovery_code": 1,
        "secret_assignment": 2,
        "sk_token": 1,
    }
    assert rejected[0][3]["extractor_hash"] == PROMPT_HASH
    serialized_detail = json.dumps(rejected[0][3], ensure_ascii=False)
    assert all(value not in serialized_detail for value in secret_values)


def test_secret_filter_checks_all_claim_fields_before_any_raw_field_audit() -> None:
    secret_values = [
        "它 sk-SubjectSecret123",
        "Abcdef1234567890",
        "sk-EntitySecret123456",
        "password=ReasonSecret",
    ]
    raw = json.dumps(
        {
            "claims": [
                {
                    "subject": secret_values[0],
                    "predicate": "事实",
                    "value": "安全的主语测试值",
                },
                {
                    "predicate": "事实",
                    "value": "安全的限定词测试值",
                    "qualifiers": {"credential": secret_values[1]},
                },
                {
                    "predicate": "事实",
                    "value": "安全的实体测试值",
                    "entities": [secret_values[2]],
                },
                {
                    "predicate": "事实",
                    "value": "安全的原因测试值",
                    "reason": secret_values[3],
                },
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            _FakeLLMClient(raw),
            ChunkingPolicy(10_000, 0, 2),
        ).extract("结构化字段中包含凭据")

    assert claims == []
    rejected = [event for event in audit.events if event[1] == "secret_rejected"]
    assert len(rejected) == 1
    assert rejected[0][3]["count"] == len(secret_values)
    serialized_events = json.dumps(audit.events, ensure_ascii=False)
    assert all(value not in serialized_events for value in secret_values)


def test_secret_filter_checks_mapping_keys_and_quoted_assignments() -> None:
    secret_values = [
        "sk-KeySecret123",
        "abcde-fghij",
        '配置 {"api_key":"hunter2"}',
    ]
    raw = json.dumps(
        {
            "claims": [
                {
                    "predicate": "事实",
                    "value": "安全的键名测试值",
                    "qualifiers": {secret_values[0]: True},
                },
                {
                    "predicate": "事实",
                    "value": "安全的恢复码键名测试值",
                    "qualifiers": {secret_values[1]: True},
                },
                {
                    "predicate": "配置",
                    "value": secret_values[2],
                },
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )
    audit = _RecordingAudit()

    with audit_scope(audit):
        claims = LLMExtractor(
            _FakeLLMClient(raw),
            ChunkingPolicy(10_000, 0, 2),
        ).extract("结构化键名和 JSON 赋值中包含凭据")

    assert claims == []
    rejected = [event for event in audit.events if event[1] == "secret_rejected"]
    assert len(rejected) == 1
    assert rejected[0][3]["reason_counts"] == {
        "recovery_code": 1,
        "secret_assignment": 1,
        "sk_token": 1,
    }
    serialized_events = json.dumps(audit.events, ensure_ascii=False)
    assert all(value not in serialized_events for value in secret_values)


def test_recovery_code_filter_requires_a_code_and_preserves_safe_policy_text() -> None:
    raw = json.dumps(
        {
            "claims": [
                {
                    "predicate": "事实",
                    "value": "abcde-fghij",
                    "confidence": 0.9,
                },
                {
                    "predicate": "配置",
                    "value": "安全政策禁止保存 recovery codes",
                    "confidence": 0.9,
                },
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )

    claims = LLMExtractor(
        _FakeLLMClient(raw),
        ChunkingPolicy(10_000, 0, 2),
    ).extract("abcde-fghij\n安全政策禁止保存 recovery codes")

    assert [claim.value for claim in claims] == ["安全政策禁止保存 recovery codes"]


def test_debug_metrics_reflect_all_claims_rejected_after_llm_response(caplog) -> None:
    raw = json.dumps(
        {
            "claims": [
                {
                    "predicate": "事实",
                    "value": "服务令牌是 sk-DebugSecret123",
                    "reason": "用户直接陈述",
                }
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )
    extractor = LLMExtractor(_FakeLLMClient(raw), ChunkingPolicy(10_000, 0, 2))

    with caplog.at_level(logging.DEBUG, logger="hl_mem.ingest.llm_extractor"):
        claims = extractor.extract("包含凭据的事件")

    payload = json.loads(caplog.records[-1].message)
    assert claims == []
    assert payload["should_memorize"] is False
    assert payload["claims_count"] == 0
    assert payload["reason"] == "postprocess_rejected"


def test_unsettled_claim_confidence_is_capped_but_confirmed_claim_is_preserved() -> None:
    raw = json.dumps(
        {
            "claims": [
                {
                    "predicate": "配置",
                    "value": "建议将索引保留周期改为两天",
                    "confidence": 0.9,
                },
                {
                    "predicate": "配置",
                    "value": "或许可以考虑后续更换测试框架",
                    "confidence": 0.4,
                },
                {
                    "predicate": "配置",
                    "value": "已确认采纳该建议：CI 失败只重跑一次",
                    "confidence": 0.9,
                },
                {
                    "predicate": "配置",
                    "value": "该建议已执行：CI 现在使用 -v -x",
                    "confidence": 0.85,
                },
            ],
            "should_memorize": True,
        },
        ensure_ascii=False,
    )

    claims = LLMExtractor(
        _FakeLLMClient(raw),
        ChunkingPolicy(10_000, 0, 2),
    ).extract(
        "\n".join(
            [
                "建议将索引保留周期改为两天",
                "或许可以考虑后续更换测试框架",
                "已确认采纳该建议：CI 失败只重跑一次",
                "该建议已执行：CI 现在使用 -v -x",
            ]
        )
    )

    assert [claim.confidence for claim in claims] == [0.55, 0.4, 0.9, 0.85]
