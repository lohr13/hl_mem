"""API 请求模型。集中定义事件、召回、记忆、Episode 与反馈接口的 Pydantic DTO。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hl_mem.application.answerability import Answerability
from hl_mem.domain.recall import RecallIntent
from hl_mem.recall.injection import DEFAULT_POLICY_VERSIONS


class NamespaceInput(BaseModel):
    """单租户部署内的相关性软分区输入。"""

    namespace: str = Field(default="default", min_length=1, max_length=100)
    # 兼容旧请求；namespace 只是软标签，不是授权或数据隔离边界。
    tenant_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        deprecated=True,
        description="Deprecated compatibility alias for namespace; not a security boundary.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_namespace_aliases(cls, data: Any) -> Any:
        """同时提供两个名称时要求值完全一致。"""
        if isinstance(data, dict):
            namespace = data.get("namespace")
            tenant_id = data.get("tenant_id")
            if namespace is not None and tenant_id is not None and namespace != tenant_id:
                raise ValueError("namespace and deprecated tenant_id must match")
        return data

    @property
    def effective_namespace(self) -> str:
        """返回兼容 alias 解析后的 namespace。"""
        return str(self.__dict__.get("tenant_id") or self.namespace)


class EventInput(NamespaceInput):
    """事件写入请求。"""

    id: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    user_id: str | None = Field(default=None, max_length=100)
    project_id: str | None = Field(default=None, max_length=100)
    agent_id: str | None = Field(default=None, max_length=100)
    session_id: str | None = Field(default=None, max_length=200)
    event_type: str = Field(default="message", max_length=50)
    actor_type: str = Field(default="user", max_length=50)
    actor_id: str | None = Field(default=None, max_length=100)
    content: dict[str, Any] | str = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None
    occurred_at: str | None = None
    source_uri: str | None = Field(default=None, max_length=2000)
    sensitivity: str = Field(default="normal", max_length=20)


class EventBatchInput(BaseModel):
    """单次原子写入的有界 Event 集合。"""

    events: list[EventInput] = Field(min_length=1, max_length=4)


class DryRunExtractionInput(BaseModel):
    """Dry-run 提取请求，不触发任何记忆持久化。"""

    text: str = Field(min_length=1, max_length=50000)
    context: dict[str, Any] = Field(default_factory=dict)
    custom_instructions: str | None = Field(default=None, max_length=10000)


class ConsolidationScopeInput(BaseModel):
    """手动归并任务的显式作用域。"""

    namespace: str = Field(default="default", min_length=1, max_length=100)
    slot_filter: str | None = Field(default=None, max_length=200)
    tag_filter: list[str] | None = None
    max_pairs: int = Field(default=500, ge=1)
    similarity_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    similarity_ceiling: float = Field(default=0.95, ge=0.0, le=1.0)


class RecallInput(NamespaceInput):
    """记忆召回请求。"""

    query: str = Field(min_length=1, max_length=2000)
    limit: int | None = Field(default=None, ge=1, le=100)
    as_of: str | None = None
    session_id: str | None = Field(default=None, max_length=200)
    intent: RecallIntent | None = None
    known_as_of: str | None = None
    token_budget: int | None = Field(default=None, ge=1)
    context_mode: str | None = Field(default=None, pattern="^(packed)$")
    response_format: Literal["legacy", "context_packet", "both"] = "legacy"
    debug: bool = False


class RetrievalBundleInput(RecallInput):
    """Hermes-only injection context; excluded from the public OpenAPI contract."""

    delivery_purpose: Literal["passive_injection", "active_recall"] = "passive_injection"
    experiment_variant: str = Field(default="control", min_length=1, max_length=100)
    echo_variant: str = Field(default="off", min_length=1, max_length=100)
    freshness_variant: str = Field(default="off", min_length=1, max_length=100)
    policy_versions: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_POLICY_VERSIONS))
    rendering_now: str | None = None
    freshness_time_bucket: str | None = None


class ClaimOutput(BaseModel):
    """公开召回 Claim 的兼容输出契约。"""

    type: Literal["claim"] = "claim"
    memory_type: Literal["claim"] = "claim"
    id: str
    text: str
    score: float
    features: dict[str, float] = Field(default_factory=dict)
    equivalent_claim_ids: list[str] = Field(default_factory=list)
    status: str | None = None
    assertion_kind: Literal["unknown", "observation", "inference"] = "unknown"
    confidence: float | None = None
    canonical_attribute: str | None = Field(
        default=None,
        deprecated=True,
        description="兼容字段；新客户端应使用 canonical_slot 与 topic_tags。",
    )
    canonical_slot: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_from: str | None = None
    recorded_to: str | None = None
    occurred_start: str | None = None
    occurred_end: str | None = None
    entities: list[str] | None = None
    replacement: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    feedback_id: str | None = None
    score_path: str | None = None
    reranker_raw_score: float | None = None
    relations: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] | None = None


class ExperienceMemoryOutput(BaseModel):
    """Tool/Procedure 专用召回的统一 Experience 输出。"""

    type: Literal["policy", "episode", "trace"]
    memory_type: Literal["policy", "episode", "trace"]
    id: str
    text: str
    score: float
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)
    feedback_id: str | None = None


class ContextPacketItemOutput(BaseModel):
    """Context Packet v1 中的扁平记忆条目。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["claim", "observation", "policy", "episode", "trace"]
    id: str
    text: str
    evidence: list[dict[str, Any]]
    feedback_id: str = Field(min_length=1, pattern=r".*\S.*")
    role: str | None = Field(default=None, min_length=1, pattern=r".*\S.*")
    action: str | None = Field(default=None, min_length=1, pattern=r".*\S.*")
    object: str | None = Field(default=None, min_length=1, pattern=r".*\S.*")

    @model_validator(mode="after")
    def validate_relation_fields(self) -> "ContextPacketItemOutput":
        relation = (self.role, self.action, self.object)
        if any(value is not None for value in relation) and not all(value is not None for value in relation):
            raise ValueError("context packet relation fields must be complete")
        if self.type != "claim" and any(value is not None for value in relation):
            raise ValueError("only claim context packet items may carry relation fields")
        return self


class ContextPacketOutput(BaseModel):
    """严格冻结的 Context Packet v1 输出契约。"""

    model_config = ConfigDict(extra="forbid")

    schema_major: Literal[1]
    schema_minor: Literal[1]
    query_id: str
    answerability: Answerability
    feedback_state: Literal["available", "degraded"]
    items: list[ContextPacketItemOutput]
    used_tokens_estimate: int = Field(ge=0)
    truncated: bool


class RecallOutput(BaseModel):
    """REST 与 MCP 共享应用服务返回的召回契约。"""

    results: list[ClaimOutput | ExperienceMemoryOutput]
    observations: list[dict[str, Any]]
    policies: list[dict[str, Any]]
    total: int
    query_id: str | None = None
    answerability: Answerability | None = None
    context: dict[str, Any] | None = None
    search_trace: dict[str, Any] | None = None
    context_packet: ContextPacketOutput | None = None

    @model_validator(mode="after")
    def validate_answerability_candidates(self) -> "RecallOutput":
        """冻结 hard/soft 与公开候选集合的对应关系。"""
        has_candidates = bool(self.results or self.observations or self.policies)
        if self.answerability == "no_evidence" and has_candidates:
            raise ValueError("no_evidence requires an empty candidate set")
        if self.answerability == "low_confidence" and not has_candidates:
            raise ValueError("low_confidence requires at least one candidate")
        return self


class ContextPacketRecallOutput(BaseModel):
    """仅返回 Context Packet 的召回响应。"""

    model_config = ConfigDict(extra="forbid")

    context_packet: ContextPacketOutput


class MemoryInput(NamespaceInput):
    """显式记忆写入请求。"""

    text: str | None = Field(default=None, max_length=50000)
    content: str | None = Field(default=None, max_length=50000)
    subject: str = Field(default="用户", max_length=200)
    predicate: str = Field(default="explicit_memory", max_length=100)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)


class MemorySaveOutput(BaseModel):
    """显式记忆写入的真实幂等结果。"""

    id: str
    created: bool


class MemoryListItemOutput(BaseModel):
    """分页记忆列表中的公开 Claim 摘要。"""

    id: str
    text: str
    status: str
    assertion_kind: Literal["unknown", "observation", "inference"] = "unknown"
    recorded_from: str
    valid_from: str | None = None
    canonical_slot: str | None = None
    topic_tags: list[str] = Field(default_factory=list)


class MemoryListOutput(BaseModel):
    """稳定 offset 分页的记忆列表响应。"""

    memories: list[MemoryListItemOutput]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class MemoryDetailOutput(BaseModel):
    """单条 Claim 的内容、生命周期、来源与冲突历史。"""

    id: str
    text: str
    namespace: str
    subject: str | None = None
    predicate: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    status: str
    assertion_kind: Literal["unknown", "observation", "inference"] = "unknown"
    confidence: float | None = None
    importance: float | None = None
    scope: str | None = None
    recorded_from: str
    recorded_to: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    expires_at: str | None = None
    canonical_attribute: str | None = None
    canonical_slot: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    superseded_by_id: str | None = None
    evidence_links: list[dict[str, Any]] = Field(default_factory=list)
    source_events: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class MemoryCorrectionInput(BaseModel):
    """按目标 Claim 标识执行仅内容替换。"""

    corrected_text: str = Field(min_length=1, max_length=50000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class MemoryCorrectionOutput(BaseModel):
    """显式纠正事件与新 Claim 标识。"""

    correction_event_id: str
    new_claim_id: str
    created: bool


class ConflictCandidateOutput(BaseModel):
    """组级审核快照中的一个 canonical candidate。"""

    candidate_key: str
    canonical_value: Any
    representative_claim_id: str
    support_count: int = Field(ge=1)
    evidence_count: int = Field(ge=0)
    first_seen_at: str
    last_seen_at: str
    claim_ids: list[str] = Field(default_factory=list)
    claim_statuses: dict[str, str] = Field(default_factory=dict)


class ConflictReviewOutput(BaseModel):
    """带 generation/revision 的完整组级人工审核快照。"""

    case_id: str
    namespace: str | None = None
    group_key: str | None = None
    generation: int = Field(ge=1)
    revision: int = Field(ge=0)
    status: str
    overflow: bool
    candidate_count: int = Field(ge=0)
    candidates: list[ConflictCandidateOutput] = Field(default_factory=list)


class ConflictResolutionInput(BaseModel):
    """基于审核 revision 的组级候选操作。"""

    action: Literal["select_candidate", "reject_candidate"]
    candidate_key: str = Field(min_length=1, max_length=50000)
    expected_revision: int = Field(ge=0)
    rationale: str | None = Field(default=None, max_length=5000)


class ConflictResolutionOutput(BaseModel):
    """组级候选操作结果。"""

    case_id: str
    generation: int = Field(ge=1)
    revision: int = Field(ge=0)
    status: str
    action: str
    candidate_key: str
    winner_id: str | None = None
    resolved_at: str | None = None


class ErrorOutput(BaseModel):
    """REST 错误响应。"""

    detail: str


class ConflictEvidenceOutput(BaseModel):
    """Claim 的有界证据链接及可用的来源事件摘要。"""

    id: str
    evidence_type: str
    evidence_id: str
    relation: str
    weight: float | None = None
    event_type: str | None = None
    occurred_at: str | None = None
    content_json: str | None = None


class ConflictDossierClaimOutput(BaseModel):
    """宿主 agent 裁决所需的完整 Claim 字段。"""

    id: str
    canonical_slot: str | None = None
    value: Any
    subject_entity_id: str | None = None
    assertion_kind: str
    confidence: float | None = None
    source_authority: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    observed_at: str | None = None
    status: str
    evidence_links: list[ConflictEvidenceOutput] = Field(default_factory=list)


class ConflictDossierCandidateOutput(BaseModel):
    """group case 的 canonical candidate 及其全部成员 Claim。"""

    candidate_key: str
    representative_claim_id: str
    support_count: int = Field(ge=1)
    canonical_value_json: str
    member_claims: list[ConflictDossierClaimOutput] = Field(default_factory=list)


class ConflictDossierOutput(BaseModel):
    """pair/group 共用的宿主裁决案卷。"""

    case_id: str
    pair_key: str
    status: str
    created_at: str
    revision: int = Field(ge=0)
    namespace_key: str | None = None
    group_key: str | None = None
    overflow: bool
    left_claim: ConflictDossierClaimOutput
    right_claim: ConflictDossierClaimOutput
    candidates: list[ConflictDossierCandidateOutput] = Field(default_factory=list)


class ConflictCaseSummaryOutput(BaseModel):
    """未闭合冲突列表中的轮询摘要。"""

    case_id: str
    status: str
    created_at: str
    namespace: str
    group_key: str | None = None
    slot: str | None = None
    revision: int = Field(ge=0)


class ConflictCaseListOutput(BaseModel):
    """分页的未闭合冲突列表。"""

    cases: list[ConflictCaseSummaryOutput] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class EpisodeInput(NamespaceInput):
    """创建 Episode 的请求。"""

    goal: str = Field(min_length=1, max_length=5000)
    session_id: str | None = Field(default=None, max_length=200)
    task_type: str | None = Field(default=None, max_length=50)


class TraceInput(BaseModel):
    """追加 Episode Trace 的请求。"""

    action: str = Field(min_length=1, max_length=10000)
    observation: str | None = Field(default=None, max_length=1000)
    error_signature: str | None = Field(default=None, max_length=500)
    value: float = 0.0


class EpisodeUpdate(BaseModel):
    """更新 Episode 结果的请求。"""

    status: str | None = None
    reward: float | None = Field(default=None, ge=0.0, le=1.0)
    outcome_summary: str | None = None


class ExplicitCorrectionInput(BaseModel):
    """仅由显式授权字段触发的记忆纠正。"""

    memory_type: Literal["claim"]
    memory_id: str = Field(min_length=1, max_length=200)
    corrected_text: str | None = Field(default=None, max_length=50000)
    action: Literal["retract", "replace"]
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class FeedbackInput(BaseModel):
    """检索结果反馈请求。"""

    feedback_id: str = Field(min_length=1, max_length=200)
    helpful: bool
    task_outcome: float | None = Field(default=None, ge=0.0, le=1.0)
    correction: ExplicitCorrectionInput | None = None
