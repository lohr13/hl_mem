"""Governed Reranker Provider adapter, facade, and explicit fake."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from hl_mem.errors import ProviderCallError
from hl_mem.observability.usage import UsageAmount
from hl_mem.plugins.contracts import (
    ProviderEndpoint,
    ProviderFactoryContext,
    ProviderRequest,
    ProviderResponse,
    RerankerProviderAdapter,
    RerankInvocation,
)
from hl_mem.plugins.contracts import RerankResult as ProviderRerankResult
from hl_mem.plugins.proxies import GovernedProviderCall


class InvalidRerankResponse(ValueError):
    """The Provider response cannot be represented by the stable result contract."""


class InvalidResultIndex(ValueError):
    """A Provider returned an index outside the host-supplied document set."""


@dataclass
class RerankResult:
    results: list[tuple[int, float]] = field(default_factory=list)
    outcome: str = "empty"
    error_class: str | None = None


class DashScopeRerankerProvider:
    """Translate neutral rerank calls to DashScope's native endpoint."""

    name = "dashscope"

    def build_request(self, endpoint: ProviderEndpoint, invocation: RerankInvocation) -> ProviderRequest:
        return ProviderRequest(
            "POST",
            f"{endpoint.base_url.rstrip('/')}/api/v1/services/rerank/text-rerank/text-rerank",
            {"Authorization": f"Bearer {endpoint.api_key}"},
            {
                "model": endpoint.model,
                "input": {"query": invocation.query, "documents": list(invocation.documents)},
                "parameters": {"top_n": invocation.top_n, "return_documents": False},
            },
            endpoint.timeout_seconds,
            endpoint.connect_timeout_seconds,
        )

    def parse_response(self, response: ProviderResponse) -> ProviderRerankResult:
        try:
            output = response.json_body.get("output")
            if not isinstance(output, Mapping):
                raise InvalidRerankResponse("reranker response output must be an object")
            raw_results = output.get("results")
            if not isinstance(raw_results, (list, tuple)):
                raise InvalidRerankResponse("reranker response results must be an array")
            results: list[tuple[int, float]] = []
            for item in raw_results:
                if not isinstance(item, Mapping):
                    raise InvalidRerankResponse("reranker result must be an object")
                raw_index = item.get("index")
                raw_score = item.get("relevance_score")
                if type(raw_index) is not int:
                    raise InvalidRerankResponse("reranker result index must be an integer")
                if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                    raise InvalidRerankResponse("reranker relevance score must be numeric")
                score = float(raw_score)
                if not math.isfinite(score):
                    raise InvalidRerankResponse("reranker relevance score must be finite")
                results.append((raw_index, score))
            usage = response.json_body.get("usage")
            input_tokens: int | None = None
            output_tokens: int | None = None
            if isinstance(usage, Mapping):
                if usage.get("input_tokens") is not None:
                    input_tokens = int(usage["input_tokens"])
                if usage.get("output_tokens") is not None:
                    output_tokens = int(usage["output_tokens"])
                if (input_tokens is not None and input_tokens < 0) or (output_tokens is not None and output_tokens < 0):
                    raise InvalidRerankResponse("reranker token usage must be non-negative")
            return ProviderRerankResult(tuple(results), input_tokens, output_tokens)
        except InvalidRerankResponse:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidRerankResponse("reranker response envelope is invalid") from error


class DashScopeReranker:
    """Gracefully contain invalid results while exposing transport failures to recall."""

    def __init__(
        self,
        *,
        endpoint: ProviderEndpoint,
        provider: RerankerProviderAdapter,
        governed: GovernedProviderCall[list[tuple[int, float]]],
        owned_runtime: object | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = endpoint.api_key
        self.base_url = endpoint.base_url.rstrip("/")
        self.model = endpoint.model
        self.timeout = endpoint.timeout_seconds
        self.max_attempts = endpoint.max_attempts
        self.provider = provider
        self._governed = governed
        self._owned_runtime = owned_runtime
        self.last_outcome = "empty"
        self.last_error_class: str | None = None
        self.last_result = RerankResult()

    def close(self) -> None:
        runtime = self._owned_runtime
        if runtime is not None:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()
            self._owned_runtime = None

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        if not documents:
            self._set_result([], "empty", None)
            return []
        invocation = RerankInvocation(query, tuple(documents), top_n)
        estimate = UsageAmount(requests=1, rerank_documents=len(documents))
        usage_status = "usage_unknown"

        def parse(response: ProviderResponse) -> tuple[list[tuple[int, float]], UsageAmount]:
            nonlocal usage_status
            parsed = self.provider.parse_response(response)
            ranked = self._validate_results(parsed.results, document_count=len(documents))
            if parsed.input_tokens is not None or parsed.output_tokens is not None:
                usage_status = "success"
            return (
                ranked,
                UsageAmount(
                    requests=1,
                    input_tokens=parsed.input_tokens or 0,
                    output_tokens=parsed.output_tokens or 0,
                    rerank_documents=len(documents),
                ),
            )

        try:
            ranked = self._governed.execute_factory(
                lambda: self.provider.build_request(self.endpoint, invocation),
                estimate,
                parse,
                max_attempts=self.max_attempts,
                settlement_status=lambda _value: usage_status,
            )
        except ProviderCallError as error:
            self._set_result([], "error", error.category)
            raise
        except Exception as error:
            self._set_result([], "error", type(error).__name__)
            return []
        self._set_result(ranked, "success" if ranked else "empty", None)
        return ranked

    @staticmethod
    def _validate_results(
        results: tuple[tuple[int, float], ...],
        *,
        document_count: int,
    ) -> list[tuple[int, float]]:
        indexes: set[int] = set()
        ranked: list[tuple[int, float]] = []
        for raw_index, raw_score in results:
            if type(raw_index) is not int or raw_index < 0 or raw_index >= document_count:
                raise InvalidResultIndex("reranker result index is outside the document set")
            if raw_index in indexes:
                raise InvalidResultIndex("reranker result indexes must be unique")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise InvalidRerankResponse("reranker relevance score must be numeric")
            score = float(raw_score)
            if not math.isfinite(score):
                raise InvalidRerankResponse("reranker relevance score must be finite")
            indexes.add(raw_index)
            ranked.append((raw_index, score))
        return sorted(ranked, key=lambda item: item[1], reverse=True)

    def _set_result(self, results: list[tuple[int, float]], outcome: str, error_class: str | None) -> None:
        self.last_outcome = outcome
        self.last_error_class = error_class
        self.last_result = RerankResult(results, outcome, error_class)


class FakeReranker:
    """Explicit test stub that returns input order with decreasing scores."""

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        results = [(index, 1.0 - index * 0.01) for index in range(min(len(documents), top_n))]
        self.last_outcome = "success" if results else "empty"
        self.last_error_class = None
        self.last_result = RerankResult(results, self.last_outcome)
        return results


def make_builtin_reranker_provider(context: ProviderFactoryContext) -> DashScopeRerankerProvider:
    if context.key.name != "dashscope":
        raise ValueError(f"unsupported built-in Reranker provider {context.key.name!r}")
    return DashScopeRerankerProvider()


Reranker = DashScopeReranker

__all__ = [
    "DashScopeReranker",
    "DashScopeRerankerProvider",
    "FakeReranker",
    "InvalidRerankResponse",
    "InvalidResultIndex",
    "RerankResult",
    "Reranker",
    "make_builtin_reranker_provider",
]
