from __future__ import annotations

import httpx


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

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        """Return document indexes and relevance scores, or an empty list on failure."""
        if not documents:
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
                return []
            return sorted(ranked, key=lambda item: item[1], reverse=True)
        except Exception:
            return []


class FakeReranker:
    """Test stub: returns input order with decreasing fake scores."""

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]:
        return [(i, 1.0 - i * 0.01) for i in range(min(len(documents), top_n))]
