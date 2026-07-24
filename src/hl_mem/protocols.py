"""HL-Mem 核心可替换组件的结构化接口协议。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypedDict

from hl_mem.domain.recall import RecallIntent

if TYPE_CHECKING:
    from hl_mem.ingest.extractors import ExtractedClaim


class ClaimRow(TypedDict, total=False):
    """检索链路使用的已解码 Claim 行。"""

    id: str
    namespace_key: str
    subject_entity_id: str
    predicate: str
    value: object
    status: str
    confidence: float
    canonical_attribute: str | None
    canonical_slot: str | None
    topic_tags: list[str]
    embedding_dense: bytes
    valid_from: str | None
    valid_to: str | None
    recorded_from: str | None
    recorded_to: str | None
    access_count: int
    helpful_rate: float


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
    ) -> list[ExtractedClaim]: ...


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
        intent: RecallIntent,
        known_as_of: str | None,
        namespace: str,
    ) -> list[ClaimRow]: ...


class VectorSearchBackend(Protocol):
    """向量检索后端协议。"""

    def search(
        self,
        query_blob: bytes,
        limit: int,
        reference_time: str,
        intent: RecallIntent,
        known_as_of: str | None,
        namespace: str,
    ) -> list[ClaimRow]: ...
