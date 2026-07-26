"""HL-Mem 核心可替换组件的结构化接口协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

from hl_mem.domain.recall import RecallIntent

if TYPE_CHECKING:
    from hl_mem.domain.content import ImagePart
    from hl_mem.ingest.extractors import ExtractedClaim

MemoryType = Literal["claim", "observation", "policy", "episode", "trace"]


@dataclass(frozen=True)
class IntentDecision:
    """可选意图路由器返回的受限决策。"""

    intent: RecallIntent
    confidence: float
    rationale_code: str


class IntentRouterProtocol(Protocol):
    """在确定性规则无强信号时提供可选意图判定。"""

    def route(
        self,
        query: str,
        *,
        allowed: tuple[RecallIntent, ...],
        timeout_seconds: float,
    ) -> IntentDecision: ...


@dataclass(frozen=True)
class UsefulnessSnapshot:
    """基于检索反馈聚合的有界 usefulness 状态。"""

    memory_type: MemoryType
    memory_id: str
    helpful_count: int
    unhelpful_count: int
    success_sum: float
    outcome_count: int
    usefulness_score: float
    retention_bonus_days: int
    updated_at: str


class UsefulnessPolicyProtocol(Protocol):
    """从反馈计数计算 usefulness 和保留奖励。"""

    def evaluate(
        self,
        *,
        helpful_count: int,
        unhelpful_count: int,
        success_sum: float,
        outcome_count: int,
    ) -> tuple[float, int]: ...


@dataclass(frozen=True)
class ExplicitCorrection:
    """由用户显式授权的记忆纠正动作。"""

    memory_type: MemoryType
    memory_id: str
    corrected_text: str | None
    action: Literal["retract", "replace"]
    idempotency_key: str


@dataclass(frozen=True)
class ImageLocator:
    """图片在原始证据中的稳定定位信息。"""

    uri: str | None
    media_type: str
    sha256: str | None
    page: int | None = None
    region: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ImageDescription:
    """视觉模型返回的可审计派生文本。"""

    caption: str
    ocr_text: str
    model: str
    confidence: float | None
    locator: ImageLocator


class ImageDescriberProtocol(Protocol):
    """把图片证据转换为 caption/OCR。"""

    def describe(self, image: ImagePart, *, timeout_seconds: float) -> ImageDescription: ...


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
    score: float
    features: dict[str, float]


class RecallResult(TypedDict):
    """公开 Claim 召回结果的类型契约。"""

    type: str
    memory_type: str
    id: str
    text: object
    score: float
    features: dict[str, float]
    status: str
    confidence: float | None
    canonical_attribute: str | None
    canonical_slot: str | None
    topic_tags: list[str]
    valid_from: str | None
    evidence: list[dict[str, object]]
    relations: list[dict[str, object]]


@dataclass(frozen=True)
class RelationProposal:
    """模型提出但尚未应用的 Claim 关系。"""

    from_claim_id: str
    to_claim_id: str
    relation: str
    confidence: float
    rationale: str
    supporting_claim_ids: tuple[str, ...]
    model: str


class RelationDiscoveryProtocol(Protocol):
    """从有界 Claim 候选池中提出关系。"""

    def propose(
        self,
        source_claim: ClaimRow,
        candidates: list[ClaimRow],
        *,
        max_proposals: int,
    ) -> list[RelationProposal]: ...


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


@dataclass(frozen=True)
class QueryExpansion:
    """单条查询改写及其可审计来源。"""

    text: str
    source: str
    weight: float = 0.6


@dataclass(frozen=True)
class QueryExpansionResult:
    """查询扩展结果及模型调用用量。"""

    expansions: tuple[QueryExpansion, ...]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    outcome: str = "applied"


class QueryExpansionProtocol(Protocol):
    """生成受限语义改写，不改变查询约束。"""

    def expand(
        self,
        query: str,
        *,
        intent: RecallIntent,
        max_expansions: int,
        timeout_seconds: float,
        token_ceiling: int,
        source: str | None = None,
    ) -> QueryExpansionResult: ...


@dataclass(frozen=True)
class WeightedQuery:
    """携带来源和融合权重的召回查询。"""

    text: str
    source: str
    weight: float
