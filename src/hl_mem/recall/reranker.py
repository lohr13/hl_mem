from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import httpx

from hl_mem.errors import ConfigurationError
from hl_mem.protocols import RerankerProtocol
from hl_mem.settings import Settings


@dataclass
class RerankResult:
    results: list[tuple[int, float]] = field(default_factory=list)
    outcome: str = "empty"
    error_class: str | None = None


class DashScopeReranker:
    """DashScope gte-rerank-v2 client, HTTP only, graceful degradation."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com",
        model: str = "gte-rerank-v2",
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = client
        self.last_outcome = "empty"
        self.last_error_class: str | None = None
        self.last_result = RerankResult()

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        """Return document indexes and relevance scores, or an empty list on failure."""
        if not documents:
            self.last_outcome, self.last_error_class = "empty", None
            self.last_result = RerankResult([], self.last_outcome)
            return []
        try:
            post = self._client.post if self._client is not None else httpx.post
            response = post(
                f"{self.base_url}/api/v1/services/rerank/text-rerank/text-rerank",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "input": {"query": query, "documents": documents},
                    "parameters": {"top_n": top_n, "return_documents": False},
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            results = response.json()["output"]["results"]
            ranked = [(int(item["index"]), float(item["relevance_score"])) for item in results]
            if any(index < 0 or index >= len(documents) for index, _ in ranked):
                self.last_outcome, self.last_error_class = "error", "InvalidResultIndex"
                self.last_result = RerankResult([], self.last_outcome, self.last_error_class)
                return []
            ranked = sorted(ranked, key=lambda item: item[1], reverse=True)
            self.last_outcome, self.last_error_class = ("success" if ranked else "empty"), None
            self.last_result = RerankResult(ranked, self.last_outcome)
            return ranked
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            self.last_outcome, self.last_error_class = "error", type(error).__name__
            self.last_result = RerankResult([], self.last_outcome, self.last_error_class)
            return []


class FakeReranker:
    """Test stub: returns input order with decreasing fake scores."""

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        results = [(i, 1.0 - i * 0.01) for i in range(min(len(documents), top_n))]
        self.last_outcome = "success" if results else "empty"
        self.last_error_class = None
        self.last_result = RerankResult(results, self.last_outcome)
        return results


Reranker = DashScopeReranker

RERANKER_PROVIDERS: dict[str, Callable[..., RerankerProtocol]] = {
    "dashscope": DashScopeReranker,
}


def make_reranker(
    settings: Settings,
    provider_types: dict[str, Callable[..., RerankerProtocol]] | None = None,
) -> RerankerProtocol | None:
    """根据模式与 provider registry 创建重排器，并保留开发环境降级策略。"""
    if settings.reranker_mode == "off":
        return None
    if settings.reranker_mode == "fake":
        return FakeReranker()
    if not settings.reranker_api_key:
        if settings.environment == "production" or not settings.allow_fake_fallback:
            raise ConfigurationError(
                f"HL_MEM_RERANKER={settings.reranker_mode} but "
                "RERANKER_API_KEY or EMBEDDING_API_KEY is missing"
            )
        return None
    registry = provider_types or RERANKER_PROVIDERS
    provider_type = registry.get(settings.reranker_provider)
    if provider_type is None:
        raise ConfigurationError(f"unsupported reranker provider: {settings.reranker_provider}")
    try:
        return provider_type(
            settings.reranker_api_key,
            settings.reranker_base_url,
            settings.reranker_model,
        )
    except Exception:
        if settings.environment == "production":
            raise
        return None
