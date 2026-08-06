"""受控多查询召回的单元测试。"""

from __future__ import annotations

import json
import threading
import time

import httpx

from hl_mem.domain.recall import RecallIntent
from hl_mem.llm.types import LLMResponse
from hl_mem.protocols import WeightedQuery
from hl_mem.recall.query_expansion import QueryExpander
from hl_mem.recall.staged_pipeline import RRF_K, _weighted_rrf_scores
from hl_mem.recall.trace import (
    QueryExpansionTrace,
    SearchPhaseMetrics,
    SearchTrace,
    SearchTracer,
)


class _Client:
    model = "fake-model"

    def __init__(self, content: object, *, tokens: int = 10) -> None:
        self.content = content
        self.tokens = tokens
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return LLMResponse(
            content=self.content,
            finish_reason="stop",
            usage_total_tokens=self.tokens,
            input_tokens=6,
            output_tokens=4,
        )


def test_auto_trigger_boundaries_and_coreference() -> None:
    assert QueryExpander.trigger_for("用户名", "auto") is None
    assert QueryExpander.trigger_for("之前讨论的那个生产环境部署方案", "auto") is None
    assert (
        QueryExpander.trigger_for(
            "之前讨论的那个生产环境部署方案",
            "auto",
            context_available=True,
        )
        == "coreference"
    )
    assert QueryExpander.trigger_for("普通且足够具体的查询文本", "off") is None
    assert QueryExpander.trigger_for("普通且足够具体的查询文本", "always") == "always"
    assert (
        QueryExpander.trigger_for("普通且足够具体的查询文本", "auto", candidate_count=7, candidate_floor=8)
        == "low_recall"
    )


def test_expander_cleans_deduplicates_and_limits_results() -> None:
    client = _Client({"queries": [" 原查询 ", "Ａ方案", "A方案", "", "第二条", "第三条"]})
    result = QueryExpander(client).expand(
        "原查询",
        intent=RecallIntent.CURRENT_STATE,
        max_expansions=2,
        timeout_seconds=1.0,
        token_ceiling=256,
        source="short_query",
    )

    assert [item.text for item in result.expansions] == ["A方案", "第二条"]
    assert all(item.source == "llm_short" and item.weight == 0.6 for item in result.expansions)
    prompt = json.dumps(
        client.requests[0].messages,
        default=lambda value: value.__dict__,
        ensure_ascii=False,
    )
    assert "禁止添加人物" in prompt
    assert "namespace" in prompt


def test_expander_prompt_names_queries_output_contract() -> None:
    client = _Client({"queries": ["改写查询"]})
    QueryExpander(client).expand(
        "原查询",
        intent=RecallIntent.CURRENT_STATE,
        max_expansions=2,
        timeout_seconds=1.0,
        token_ceiling=256,
    )

    system_prompt = client.requests[0].messages[0].content
    assert '{"queries": ["改写查询 1", "改写查询 2"]}' in system_prompt
    assert "queries 必须是仅包含字符串的数组" in system_prompt
    assert "最多包含 2 项" in system_prompt


def test_expander_invalid_json_and_token_ceiling_fall_back() -> None:
    invalid = QueryExpander(_Client("not-json")).expand(
        "查询",
        intent=RecallIntent.CURRENT_STATE,
        max_expansions=2,
        timeout_seconds=1.0,
        token_ceiling=256,
    )
    over = QueryExpander(_Client({"queries": ["改写"]}, tokens=300)).expand(
        "查询",
        intent=RecallIntent.CURRENT_STATE,
        max_expansions=2,
        timeout_seconds=1.0,
        token_ceiling=256,
    )

    assert invalid.expansions == () and invalid.outcome == "error"
    assert over.expansions == () and over.outcome == "token_ceiling"


def test_expander_timeout_falls_back_without_waiting_for_client() -> None:
    class SlowClient(_Client):
        def complete(self, request):
            time.sleep(0.2)
            return super().complete(request)

    started = time.monotonic()
    result = QueryExpander(SlowClient({"queries": ["改写"]})).expand(
        "查询",
        intent=RecallIntent.CURRENT_STATE,
        max_expansions=2,
        timeout_seconds=0.01,
        token_ceiling=256,
    )

    assert result.expansions == () and result.outcome == "timeout"
    assert time.monotonic() - started < 0.15
    assert result.error_class == "deadline_timeout"


def test_expander_classifies_http_timeout_and_rate_limit() -> None:
    class FailedClient(_Client):
        def __init__(self, error):
            super().__init__({})
            self.error = error

        def complete(self, request):
            raise self.error

    request = httpx.Request("POST", "https://provider.invalid")
    timeout = QueryExpander(FailedClient(httpx.ReadTimeout("slow", request=request))).expand(
        "query",
        intent=RecallIntent.CURRENT_STATE,
        timeout_seconds=0.2,
        token_ceiling=256,
    )
    response = httpx.Response(429, request=request, json={"code": "Throttled"})
    limited = QueryExpander(FailedClient(httpx.HTTPStatusError("limited", request=request, response=response))).expand(
        "query",
        intent=RecallIntent.CURRENT_STATE,
        timeout_seconds=0.2,
        token_ceiling=256,
    )

    assert timeout.error_class == "http_timeout"
    assert limited.error_class == "rate_limit" and limited.http_status == 429


def test_expander_reuses_bounded_executor_threads() -> None:
    """连续扩展不得为每个请求创建一个新线程。"""
    client = _Client({"queries": ["改写"]})
    expander = QueryExpander(client, max_concurrency=2)

    for _ in range(6):
        result = expander.expand(
            "查询",
            intent=RecallIntent.CURRENT_STATE,
            timeout_seconds=1.0,
            token_ceiling=256,
        )
        assert result.outcome == "applied"

    worker_names = {thread.name for thread in threading.enumerate() if thread.name.startswith("query-expansion")}
    assert len(worker_names) <= 2


def test_expander_rejects_calls_beyond_concurrency_limit() -> None:
    """并发额度耗尽时必须立即拒绝，不能继续排队堆积。"""
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient(_Client):
        def complete(self, request):
            entered.set()
            release.wait(timeout=1.0)
            return super().complete(request)

    expander = QueryExpander(BlockingClient({"queries": ["改写"]}), max_concurrency=1)
    first_result: list[object] = []
    first = threading.Thread(
        target=lambda: first_result.append(
            expander.expand(
                "第一个",
                intent=RecallIntent.CURRENT_STATE,
                timeout_seconds=0.5,
                token_ceiling=256,
            )
        )
    )
    first.start()
    assert entered.wait(timeout=0.2)

    rejected = expander.expand(
        "第二个",
        intent=RecallIntent.CURRENT_STATE,
        timeout_seconds=0.5,
        token_ceiling=256,
    )
    release.set()
    first.join(timeout=1.0)

    assert rejected.outcome == "concurrency_limit"
    assert first_result


def test_weighted_rrf_uses_query_and_channel_weights() -> None:
    first = {"id": "a"}
    second = {"id": "b"}
    scores = _weighted_rrf_scores(
        [([first, second], 1.0, 1.0), ([second, first], 0.6, 1.0)],
        RRF_K,
    )

    assert scores["a"] == 1.0 / 61 + 0.6 / 62
    assert scores["b"] == 1.0 / 62 + 0.6 / 61


def test_trace_serializes_expansion_without_changing_legacy_defaults() -> None:
    trace = SearchTrace("q", "hash", "current_state", 5, 50, {}, SearchPhaseMetrics())
    assert SearchTracer(trace).to_dict()["expansions"] == []
    trace.expansions.append(QueryExpansionTrace.from_text("x" * 300, "llm_short", 0.6, input_tokens=2))

    payload = SearchTracer(trace).to_dict()
    assert len(payload["expansions"][0]["expansion_text"]) == 256
    assert len(payload["expansions"][0]["text_hash"]) == 64


def test_weighted_query_is_frozen_value_type() -> None:
    assert WeightedQuery("原查询", "original", 1.0).weight == 1.0
