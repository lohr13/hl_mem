"""HL-Mem 核心可替换组件的结构化接口协议。"""

from __future__ import annotations

from typing import Any, Protocol


class EmbedderProtocol(Protocol):
    """向量化组件协议。"""

    dim: int
    model: str

    def embed_one(self, text: str) -> bytes: ...

    def embed_batch(self, texts: list[str]) -> list[bytes]: ...


class ExtractorProtocol(Protocol):
    """记忆提取组件协议。"""

    def extract(
        self,
        content: dict[str, Any] | str,
        context: dict[str, Any] | None = None,
    ) -> list[Any]: ...


class RerankerProtocol(Protocol):
    """召回重排组件协议。"""

    def rerank(self, query: str, documents: list[str], top_n: int = 20) -> list[tuple[int, float]]: ...


class TextSearchBackend(Protocol):
    """文本检索后端协议。"""

    def search(
        self,
        query: str,
        limit: int,
        reference_time: str,
        intent: Any,
        known_as_of: str | None,
        namespace: str,
    ) -> list[dict]: ...


class VectorSearchBackend(Protocol):
    """向量检索后端协议。"""

    def search(
        self,
        query_blob: bytes,
        limit: int,
        reference_time: str,
        intent: Any,
        known_as_of: str | None,
        namespace: str,
    ) -> list[dict]: ...
