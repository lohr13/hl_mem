"""受控查询扩展：触发判断、LLM 改写与安全降级。"""

from __future__ import annotations

import json
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from hl_mem.domain.recall import RecallIntent
from hl_mem.llm.client import LLMClient, classify_provider_error
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.monitoring.alerts import CircuitBreaker
from hl_mem.protocols import QueryExpansion, QueryExpansionResult

_COREFERENCE_TERMS = ("这", "这个", "那个", "上次", "之前", "它", "他们")
_SOURCE_NAMES = {
    "short_query": "llm_short",
    "coreference": "llm_coreference",
    "low_recall": "llm_low_recall",
    "low_fts_recall": "llm_low_recall",
    "always": "llm_short",
}
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2,
        }
    },
    "required": ["queries"],
    "additionalProperties": False,
}


class QueryExpander:
    """通过 LLM 生成最多两条受约束的语义查询改写。"""

    def __init__(self, client: LLMClient, *, max_concurrency: int = 4) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.client = client
        self._executor = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="query-expansion")
        self._capacity = threading.BoundedSemaphore(max_concurrency)
        self._circuit = CircuitBreaker()

    @staticmethod
    def trigger_for(
        query: str,
        mode: str,
        *,
        candidate_count: int | None = None,
        candidate_floor: int = 8,
        fts_candidate_count: int | None = None,
        fts_candidate_floor: int = 3,
        context_available: bool = False,
    ) -> str | None:
        """返回当前模式下的扩展触发原因。"""
        if mode == "off":
            return None
        if mode == "always":
            return "always"
        normalized = unicodedata.normalize("NFKC", query).strip()
        if any(term in normalized for term in _COREFERENCE_TERMS):
            return "coreference" if context_available else None
        if candidate_count is not None and candidate_count < candidate_floor:
            return "low_recall"
        if fts_candidate_count is not None and fts_candidate_count < fts_candidate_floor:
            return "low_fts_recall"
        return None

    def expand(
        self,
        query: str,
        *,
        intent: RecallIntent,
        max_expansions: int = 2,
        timeout_seconds: float = 2.0,
        token_ceiling: int = 256,
        source: str | None = None,
        session_context: tuple[tuple[str, str], ...] = (),
    ) -> QueryExpansionResult:
        """执行一次有超时和 token 上限保护的结构化改写。"""
        started = time.perf_counter()
        if max_expansions <= 0:
            return self._empty(started, "empty")
        if not self._circuit.allow():
            return self._empty(started, "circuit_open", error_class="circuit_open")
        request = self._request(query, intent, max_expansions, session_context)
        if not self._capacity.acquire(blocking=False):
            return self._empty(started, "concurrency_limit")
        deadline = time.monotonic() + timeout_seconds
        future = self._executor.submit(self._complete_with_retry, request, deadline)
        released = threading.Event()

        def release_capacity(_future: object) -> None:
            if not released.is_set():
                released.set()
                self._capacity.release()

        future.add_done_callback(release_capacity)
        try:
            response, attempts = future.result(timeout=max(0.001, deadline - time.monotonic()))
        except FutureTimeoutError:
            future.cancel()
            release_capacity(future)
            self._circuit.record(False)
            return self._empty(started, "timeout", error_class="deadline_timeout", attempts=1)
        except Exception as error:
            self._circuit.record(False)
            error_class, http_status, provider_code = classify_provider_error(error)
            return self._empty(
                started,
                "error",
                error_class=error_class,
                attempts=int(getattr(error, "_expansion_attempts", 1)),
                http_status=http_status,
                provider_code=provider_code,
            )
        input_tokens = int(response.input_tokens or 0)
        self._circuit.record(True)
        output_tokens = int(response.output_tokens or 0)
        total_tokens = int(response.usage_total_tokens or input_tokens + output_tokens)
        if total_tokens > token_ceiling:
            return self._empty(
                started,
                "token_ceiling",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        try:
            payload = response.content if isinstance(response.content, dict) else json.loads(response.content)
            raw_queries = payload["queries"]
            if not isinstance(raw_queries, list):
                raise TypeError("queries must be a list")
            texts = self._clean_queries(query, raw_queries, max_expansions)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._empty(started, "error", input_tokens=input_tokens, output_tokens=output_tokens)
        expansion_source = _SOURCE_NAMES.get(source or "", "llm_short")
        expansions = tuple(QueryExpansion(text, expansion_source) for text in texts)
        return QueryExpansionResult(
            expansions=expansions,
            model=self.client.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
            outcome="applied" if expansions else "empty",
            attempts=attempts,
        )

    @staticmethod
    def _request(
        query: str,
        intent: RecallIntent,
        max_expansions: int,
        session_context: tuple[tuple[str, str], ...] = (),
    ) -> LLMRequest:
        system = (
            '你是查询改写器。仅输出 JSON，格式必须为：{"queries": ["改写查询 1", "改写查询 2"]}。'
            "queries 必须是仅包含字符串的数组，最多包含 2 项，且 JSON 对象不得包含其他字段。"
            "生成语义等价、便于记忆检索的查询。"
            "当原查询与记忆可能存在中英文语言错配时，在中文和英文之间翻译核心实体与关系；"
            "最多两条查询应尽量分别覆盖中文和英文，且每条查询只使用一种语言，不能把双语词堆入同一查询。"
            "禁止添加人物、时间、namespace 或原查询未给出的事实；不得改变查询意图和约束。"
        )
        context_text = ""
        if session_context:
            rendered = "\n".join(f"{role}: {text}" for role, text in session_context)
            context_text = (
                f"同一会话的先行文本（仅用于消解指代）：\n{rendered}\n"
                "只能使用这些文本中明确存在的事实，不得推断或新增事实。\n"
            )
        user = f"{context_text}原查询：{query}\n召回意图：{intent.value}\n" f"最多输出 {max_expansions} 条："
        return LLMRequest(
            messages=[LLMMessage("system", system), LLMMessage("user", user)],
            structured_output=StructuredOutputSpec(
                name="query_expansion",
                schema=_QUERY_SCHEMA,
                preferred_mode=StructuredOutputMode.JSON_SCHEMA,
            ),
        )

    @staticmethod
    def _clean_queries(original: str, values: list[Any], limit: int) -> list[str]:
        original_key = unicodedata.normalize("NFKC", original).strip().casefold()
        seen = {original_key}
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            text = unicodedata.normalize("NFKC", value).strip()
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
            if len(cleaned) >= limit:
                break
        return cleaned

    def _empty(
        self,
        started: float,
        outcome: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_class: str | None = None,
        attempts: int = 0,
        http_status: int | None = None,
        provider_code: str | None = None,
    ) -> QueryExpansionResult:
        return QueryExpansionResult(
            expansions=(),
            model=self.client.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.perf_counter() - started) * 1000,
            outcome=outcome,
            error_class=error_class,
            attempts=attempts,
            http_status=http_status,
            provider_code=provider_code,
        )

    def _complete_with_retry(self, request: LLMRequest, deadline: float) -> tuple[LLMResponse, int]:
        """向支持 deadline 的真实客户端下推本次扩展超时。"""
        for attempt in range(1, 3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("query expansion deadline exhausted")
            try:
                if isinstance(self.client, LLMClient):
                    return self.client.complete(request, timeout_seconds=remaining), attempt
                return self.client.complete(request), attempt
            except Exception as error:
                setattr(error, "_expansion_attempts", attempt)
                category, _, _ = classify_provider_error(error)
                if attempt == 2 or category not in {
                    "http_timeout",
                    "rate_limit",
                    "upstream",
                }:
                    raise
        raise RuntimeError("unreachable query expansion retry state")
