from __future__ import annotations

import httpx
from dataclasses import dataclass, field


@dataclass
class RerankResult:
    results: list[tuple[int, float]] = field(default_factory=list)
    outcome: str = "empty"
    error_class: str | None = None


class Reranker:
    """DashScope gte-rerank-v2 client, HTTP only, graceful degradation."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com",
        model: str = "gte-rerank-v2",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
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
            response = httpx.post(
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
        except Exception as error:
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
