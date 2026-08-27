"""集中化配置入口：启动时解析一次并校验配置组合。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from hl_mem.domain.claims.retention import TTLPolicy
from hl_mem.errors import ConfigurationError

EmbedderMode = Literal["fake", "real"]
EmbeddingApiMode = Literal["compatible", "native"]
EmbeddingTextType = Literal["", "document", "query"] | None
RerankerMode = Literal["off", "fake", "on", "real"]
RerankerProvider = Literal["dashscope"]
RelationExpansionMode = Literal["off", "on"]
RelationDiscoveryMode = Literal["off", "audit", "auto"]
ExtractorMode = Literal["fake", "real", "llm"]
VerificationMode = Literal["off", "audit", "enforce"]
LLMProvider = Literal["dashscope", "zhipu", "openai_compatible"]
StructuredOutputModeName = Literal["auto", "json_object", "json_schema"]
QueryExpansionMode = Literal["off", "auto", "always"]
QueryContextMode = Literal["off", "coreference"]
ProcedureRecallMode = Literal["off", "keyword", "auto"]
FeedbackLifecycleMode = Literal["off", "observe", "on"]
ExpiredCleanupMode = Literal["off", "observe", "on"]
DecayModel = Literal["legacy_linear", "activation_halflife", "confidence_halflife"]
RelevanceGateMode = Literal["off", "observe", "enforce"]
EchoSuppressionMode = Literal["off", "observe", "enforce"]
FreshnessAnnotationMode = Literal["off", "observe", "render"]
ResurrectionMode = Literal["off", "auto"]
ImageDescriberMode = Literal["off", "on"]
ImageDescriberProvider = Literal["dashscope"]
IndexTextMode = Literal["legacy", "value_only", "natural", "answerable"]
FtsLanguage = Literal["auto", "zh", "en"]
ConflictAutoMode = Literal["off", "observe", "enforce", "l0_only"]
PriceTargetMode = Literal["off", "audit", "observe", "enforce"]
PlanFulfillmentMode = Literal["off", "audit", "observe", "enforce"]
EntityConstraintMode = Literal["off", "observe", "enforce"]
LessonSignalMode = Literal["off", "observe", "enforce"]


class VectorBackend(StrEnum):
    """支持的向量检索后端。"""

    SQLITE_SCAN = "sqlite_scan"
    SQLITE_VEC = "sqlite_vec"


def is_placeholder_secret(value: str | None) -> bool:
    """判断密钥是否为空或仍为常见占位符。"""
    if value is None:
        return True
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in {"xxx", "your-key", "your_key", "changeme", "change-me"}:
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    return bool(re.fullmatch(r"(?:sk-)?x{3,}", normalized)) or (
        normalized.startswith("sk-") and normalized.endswith("xxx")
    )


def parse_daily_cron(value: str, variable_name: str) -> int:
    """严格解析每日 HH:MM 配置并返回自午夜起的分钟数。"""
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ConfigurationError(f"{variable_name} must use strict HH:MM format")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _toml_field(default: Any, path: str) -> Any:
    return field(default=default, metadata={"toml": path})


def _validate_runtime_modes(settings: "Settings") -> None:
    if settings.entity_constraint_mode not in {"off", "observe", "enforce"}:
        raise ConfigurationError("recall.entity_constraint_mode must be 'off', 'observe', or 'enforce'")
    if settings.lesson_signal_mode not in {"off", "observe", "enforce"}:
        raise ConfigurationError("extraction.lesson_signal_mode must be 'off', 'observe', or 'enforce'")
    if settings.conflict_auto_mode not in {"off", "observe", "enforce", "l0_only"}:
        raise ConfigurationError("conflict.auto_mode must be 'off', 'observe', 'enforce', or 'l0_only'")
    if settings.conflict_l1_min_time_delta_seconds not in {0, 300, 3_600}:
        raise ConfigurationError("conflict.l1_min_time_delta_seconds must be 0, 300, or 3600")
    if settings.conflict_l1_min_confidence_delta not in {0.10, 0.15, 0.20}:
        raise ConfigurationError("conflict.l1_min_confidence_delta must be 0.10, 0.15, or 0.20")
    judge_host = (urlparse(settings.maintenance_judge_base_url).hostname or "").casefold()
    if judge_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigurationError("maintenance_judge.base_url must use loopback")
    if settings.maintenance_judge_timeout_seconds <= 0:
        raise ConfigurationError("maintenance_judge.timeout_seconds must be positive")


@dataclass(frozen=True)
class Settings:
    """全局非敏感配置快照。"""

    database_path: str = field(default="var/hl_mem.db", metadata={"toml": "database.path"})
    database_pool_size: int = field(default=8, metadata={"toml": "database.pool_size"})
    database_busy_timeout_seconds: int = field(
        default=30,
        metadata={"toml": "database.busy_timeout_seconds"},
    )
    entity_aliases_path: str | None = field(default=None, metadata={"toml": "entity.aliases_path"})
    price_target_mode: PriceTargetMode = field(default="enforce", metadata={"toml": "price.target_mode"})
    plan_fulfillment_mode: PlanFulfillmentMode = field(default="enforce", metadata={"toml": "plan.fulfillment_mode"})
    latest_wins_mode: Literal["off", "observe", "enforce"] = _toml_field("observe", "state.latest_wins_mode")
    latest_wins_slots: tuple[Literal["config.version"], ...] = _toml_field(
        ("config.version",), "state.latest_wins_slots"
    )
    embedder_mode: EmbedderMode = field(default="fake", metadata={"toml": "embedding.mode"})
    embedding_dim: int = field(default=2048, metadata={"toml": "embedding.dim"})
    embedding_api_key: str | None = field(
        default=None,
        repr=False,
        metadata={"secret_env": "EMBEDDING_API_KEY"},
    )
    embedding_base_url: str = field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        metadata={"toml": "embedding.base_url"},
    )
    embedding_model: str = field(default="text-embedding-v4", metadata={"toml": "embedding.model"})
    embedding_api_mode: EmbeddingApiMode = field(
        default="compatible",
        metadata={"toml": "embedding.api_mode"},
    )
    embedding_text_type: EmbeddingTextType = _toml_field(None, "embedding.text_type")
    embedding_connect_timeout: float = _toml_field(5.0, "embedding.connect_timeout")
    embedding_read_timeout: float = field(default=30.0, metadata={"toml": "embedding.read_timeout"})
    embedding_max_attempts: int = field(default=3, metadata={"toml": "embedding.max_attempts"})
    index_text_mode: IndexTextMode = field(default="natural", metadata={"toml": "index.text_mode"})
    index_backfill_batch_size: int = field(default=100, metadata={"toml": "index.backfill_batch_size"})
    index_backfill_max_attempts: int = field(
        default=3,
        metadata={"toml": "index.backfill_max_attempts"},
    )
    index_text_version: str = field(default="v2", metadata={"toml": "index.text_version"})
    reranker_mode: RerankerMode = field(default="off", metadata={"toml": "reranker.mode"})
    reranker_provider: RerankerProvider = field(
        default="dashscope",
        metadata={"toml": "reranker.provider"},
    )
    reranker_api_key: str | None = field(
        default=None,
        repr=False,
        metadata={"secret_env": "RERANKER_API_KEY"},
    )
    reranker_base_url: str = field(
        default="https://dashscope.aliyuncs.com",
        metadata={"toml": "reranker.base_url"},
    )
    reranker_model: str = field(default="qwen3-rerank", metadata={"toml": "reranker.model"})
    relation_expansion_mode: RelationExpansionMode = field(
        default="off",
        metadata={"toml": "relation.expansion_mode"},
    )
    relation_expansion_max_depth: int = _toml_field(1, "relation.expansion_max_depth")
    # relation_discovery: audit 只记录候选 proposal，不自动写入关系边
    relation_discovery_mode: RelationDiscoveryMode = field(
        default="off",
        metadata={"toml": "relation.discovery_mode"},
    )
    relation_discovery_pool_limit: int = field(
        default=40,
        metadata={"toml": "relation.discovery_pool_limit"},
    )
    relation_discovery_max_proposals: int = field(
        default=10,
        metadata={"toml": "relation.discovery_max_proposals"},
    )
    relation_auto_apply_confidence: float = field(
        default=0.90,
        metadata={"toml": "relation.auto_apply_confidence"},
    )
    relation_conflict_confidence: float = field(
        default=0.80,
        metadata={"toml": "relation.conflict_confidence"},
    )
    recall_default_limit: int = field(default=5, metadata={"toml": "recall.default_limit"})
    fts_language: FtsLanguage = field(default="auto", metadata={"toml": "recall.fts_language"})
    recall_vector_scan_limit: int = field(default=200, metadata={"toml": "recall.vector_scan_limit"})
    recall_dense_enabled: bool = field(default=True, metadata={"toml": "recall.dense_enabled"})
    packed_context_token_budget: int = _toml_field(2000, "recall.packed_context_token_budget")
    recall_candidate_floor: int = field(default=50, metadata={"toml": "recall.candidate_floor"})
    entity_constraint_mode: EntityConstraintMode = _toml_field("observe", "recall.entity_constraint_mode")
    recall_dedup_threshold: float = field(default=0.95, metadata={"toml": "recall.dedup_threshold"})
    recall_dedup_candidate_limit: int = _toml_field(100, "recall.dedup_candidate_limit")
    echo_suppression_mode: EchoSuppressionMode = field(
        default="enforce",
        metadata={"toml": "recall.echo_suppression_mode"},
    )
    echo_session_window_seconds: int = field(
        default=1800,
        metadata={"toml": "recall.echo_session_window_seconds"},
    )
    echo_pending_review_enabled: bool = field(
        default=False,
        metadata={"toml": "recall.echo_pending_review_enabled"},
    )
    echo_pending_similarity_threshold: float = field(
        default=0.95,
        metadata={"toml": "recall.echo_pending_similarity_threshold"},
    )
    echo_pending_max_seconds: int = field(
        default=7200,
        metadata={"toml": "recall.echo_pending_max_seconds"},
    )
    freshness_annotation_mode: FreshnessAnnotationMode = field(
        default="render",
        metadata={"toml": "recall.freshness_annotation_mode"},
    )
    resurrection_mode: ResurrectionMode = field(
        default="auto",
        metadata={"toml": "recall.resurrection_mode"},
    )
    resurrection_candidate_limit: int = field(
        default=3,
        metadata={"toml": "recall.resurrection_candidate_limit"},
    )
    resurrection_min_term_coverage: float = field(
        default=0.8,
        metadata={"toml": "recall.resurrection_min_term_coverage"},
    )
    relevance_gate_mode: RelevanceGateMode = field(
        default="off",
        metadata={"toml": "recall.relevance_gate_mode"},
    )
    relevance_reranker_floor: float = field(
        default=0.15,
        metadata={"toml": "recall.relevance_reranker_floor"},
    )
    relevance_dense_floor: float = field(
        default=0.3,
        metadata={"toml": "recall.relevance_dense_floor"},
    )
    relevance_relative_drop: float = field(
        default=0.15,
        metadata={"toml": "recall.relevance_relative_drop"},
    )
    relevance_keep_top1: bool = field(default=True, metadata={"toml": "recall.relevance_keep_top1"})
    relevance_intents: tuple[str, ...] = field(
        default=("current_state",),
        metadata={"toml": "recall.relevance_intents"},
    )
    preference_recency_boost: float = field(
        default=0.12,
        metadata={"toml": "recall.preference_recency_boost"},
    )
    tag_boost_enabled: bool = field(default=True, metadata={"toml": "recall.tag_boost_enabled"})
    tag_boost_weight: float = field(default=0.05, metadata={"toml": "recall.tag_boost_weight"})
    tag_channel_enabled: bool = field(default=False, metadata={"toml": "recall.tag_channel_enabled"})
    tag_channel_weight: float = field(default=0.15, metadata={"toml": "recall.tag_channel_weight"})
    tag_candidate_limit: int = field(default=20, metadata={"toml": "recall.tag_candidate_limit"})
    # query_expansion: auto 在指代、总候选不足或 FTS 命中过少时触发受控 LLM 改写
    query_expansion_mode: QueryExpansionMode = field(
        default="auto",
        metadata={"toml": "recall.query_expansion_mode"},
    )
    query_expansion_model: str | None = field(
        default=None,
        metadata={"toml": "recall.query_expansion_model"},
    )
    query_expansion_max: int = field(default=2, metadata={"toml": "recall.query_expansion_max"})
    query_expansion_candidate_floor: int = field(
        default=8,
        metadata={"toml": "recall.query_expansion_candidate_floor"},
    )
    query_expansion_token_ceiling: int = field(
        default=256,
        metadata={"toml": "recall.query_expansion_token_ceiling"},
    )
    query_expansion_timeout_seconds: float = field(
        default=5.0,
        metadata={"toml": "recall.query_expansion_timeout_seconds"},
    )
    query_expansion_total_timeout_seconds: float = field(
        default=6.0,
        metadata={"toml": "recall.query_expansion_total_timeout_seconds"},
    )
    query_expansion_max_concurrency: int = field(
        default=4,
        metadata={"toml": "recall.query_expansion_max_concurrency"},
    )
    query_context_mode: QueryContextMode = field(default="off", metadata={"toml": "recall.query_context_mode"})
    query_context_max_events: int = field(
        default=5,
        metadata={"toml": "recall.query_context_max_events"},
    )
    query_context_token_budget: int = field(
        default=256,
        metadata={"toml": "recall.query_context_token_budget"},
    )
    # procedure_recall: keyword 为纯确定性路由，仅 TOOL/PROCEDURE intent 进入 Experience pipeline
    procedure_recall_mode: ProcedureRecallMode = field(
        default="keyword",
        metadata={"toml": "recall.procedure_mode"},
    )
    procedure_llm_threshold: float = field(
        default=0.80,
        metadata={"toml": "recall.procedure_llm_threshold"},
    )
    procedure_router_timeout_seconds: float = field(
        default=1.5,
        metadata={"toml": "recall.procedure_router_timeout_seconds"},
    )
    procedure_candidate_limit: int = field(
        default=30,
        metadata={"toml": "recall.procedure_candidate_limit"},
    )
    procedure_recent_outcome_window: int = field(
        default=20,
        metadata={"toml": "recall.procedure_recent_outcome_window"},
    )
    procedure_outcome_half_life_days: int = field(
        default=30,
        metadata={"toml": "recall.procedure_outcome_half_life_days"},
    )
    recall_side_effect_max_attempts: int = field(
        default=3,
        metadata={"toml": "recall.side_effect_max_attempts"},
    )
    recall_side_effect_backoff_seconds: float = field(
        default=0.05,
        metadata={"toml": "recall.side_effect_backoff_seconds"},
    )
    vector_backend: VectorBackend = field(
        default=VectorBackend.SQLITE_SCAN,
        metadata={"toml": "recall.vector_backend"},
    )
    vector_batch_size: int = field(default=512, metadata={"toml": "recall.vector_batch_size"})
    hermes_enabled: bool = field(default=False, metadata={"toml": "hermes.enabled"})
    hermes_url: str = field(default="http://127.0.0.1:8200", metadata={"toml": "hermes.url"})
    hermes_timeout: int = field(default=30, metadata={"toml": "hermes.timeout"})
    hermes_on_demand_recall_timeout_seconds: float = _toml_field(8.0, "hermes.on_demand_recall_timeout_seconds")
    hermes_manual_conflict_notice: bool = _toml_field(True, "hermes.manual_conflict_notice")
    hermes_home: str | None = field(default=None, metadata={"toml": "hermes.home"})
    hermes_circuit_failure_threshold: int = _toml_field(5, "hermes.circuit_failure_threshold")
    hermes_circuit_open_seconds: float = _toml_field(60.0, "hermes.circuit_open_seconds")
    hermes_prefetch_cache_ttl_seconds: float = _toml_field(300.0, "hermes.prefetch_cache_ttl_seconds")
    policy_induction_lookback_days: int = field(
        default=7,
        metadata={"toml": "worker.policy_induction_lookback_days"},
    )
    policy_induction_min_episodes: int = field(
        default=3,
        metadata={"toml": "worker.policy_induction_min_episodes"},
    )
    extractor_mode: ExtractorMode = field(default="fake", metadata={"toml": "extraction.mode"})
    extract_pre_filter: bool = field(default=False, metadata={"toml": "extraction.pre_filter"})
    verification_mode: VerificationMode = field(
        default="off",
        metadata={"toml": "extraction.verification_mode"},
    )
    lesson_signal_mode: LessonSignalMode = _toml_field("observe", "extraction.lesson_signal_mode")
    llm_api_key: str | None = field(
        default=None,
        repr=False,
        metadata={"secret_env": "LLM_API_KEY"},
    )
    llm_base_url: str = field(
        default="https://coding.dashscope.aliyuncs.com/v1",
        metadata={"toml": "llm.base_url"},
    )
    llm_model: str = field(default="qwen3.7-plus", metadata={"toml": "llm.model"})
    llm_provider: LLMProvider = field(default="dashscope", metadata={"toml": "llm.provider"})
    llm_structured_mode: StructuredOutputModeName = field(
        default="json_object",
        metadata={"toml": "llm.structured_mode"},
    )
    enable_llm_thinking: bool = field(default=False, metadata={"toml": "llm.enable_thinking"})
    llm_timeout: float = field(default=90.0, metadata={"toml": "llm.timeout"})
    llm_max_attempts: int = field(default=3, metadata={"toml": "llm.max_attempts"})
    llm_schema_retries: int = field(default=2, metadata={"toml": "llm.schema_retries"})
    image_describer_mode: ImageDescriberMode = field(
        default="off",
        metadata={"toml": "image_describer.mode"},
    )
    image_describer_provider: ImageDescriberProvider = field(
        default="dashscope",
        metadata={"toml": "image_describer.provider"},
    )
    image_describer_api_key: str | None = field(
        default=None,
        repr=False,
        metadata={"secret_env": "IMAGE_API_KEY"},
    )
    image_describer_base_url: str = field(
        default="https://coding.dashscope.aliyuncs.com/v1",
        metadata={"toml": "image_describer.base_url"},
    )
    image_describer_model: str = field(
        default="qwen3.7-plus",
        metadata={"toml": "image_describer.model"},
    )
    image_describer_timeout_seconds: float = field(
        default=20.0,
        metadata={"toml": "image_describer.timeout_seconds"},
    )
    image_max_bytes: int = field(default=10485760, metadata={"toml": "image_describer.max_bytes"})
    image_max_parts: int = field(default=4, metadata={"toml": "image_describer.max_parts"})
    image_allow_file_uris: bool = field(
        default=False,
        metadata={"toml": "image_describer.allow_file_uris"},
    )
    image_file_allow_roots: tuple[str, ...] = field(
        default=(),
        metadata={"toml": "image_describer.file_allow_roots"},
    )
    extraction_chunk_target_chars: int = _toml_field(12000, "extraction.chunk_target_chars")
    extraction_chunk_overlap_turns: int = _toml_field(2, "extraction.chunk_overlap_turns")
    extraction_max_split_depth: int = _toml_field(3, "extraction.max_split_depth")
    extraction_soft_split_enabled: bool = _toml_field(False, "extraction.soft_split_enabled")
    # Higher limits amortize LLM call cost across more same-session Events,
    # while smaller limits reduce extraction latency and per-call payload size.
    extraction_batch_max_events: int = _toml_field(5, "extraction.batch_max_events")
    # Longer waits improve the chance of filling a microbatch at the cost of
    # delaying extraction for low-traffic sessions.
    extraction_batch_max_wait_seconds: float = _toml_field(120.0, "extraction.batch_max_wait_seconds")
    worker_poll_interval: float = field(default=2.0, metadata={"toml": "worker.poll_interval"})
    worker_maintenance_interval: float = _toml_field(600.0, "worker.maintenance_interval")
    conflict_auto_resolve_enabled: bool = _toml_field(True, "worker.conflict_auto_resolve_enabled")
    conflict_auto_mode: ConflictAutoMode = _toml_field("l0_only", "conflict.auto_mode")
    conflict_l1_min_time_delta_seconds: int = _toml_field(300, "conflict.l1_min_time_delta_seconds")
    conflict_l1_min_confidence_delta: float = _toml_field(0.15, "conflict.l1_min_confidence_delta")
    conflict_maintenance_max_cases: int = _toml_field(50, "worker.conflict_maintenance_max_cases")
    conflict_maintenance_budget_ms: int = _toml_field(1_000, "worker.conflict_maintenance_budget_ms")
    conflict_failure_backoff_seconds: int = _toml_field(300, "worker.conflict_failure_backoff_seconds")
    conflict_writer_yield_ms: int = field(
        default=25,
        metadata={"toml": "worker.conflict_writer_yield_ms"},
    )
    conflict_auto_resolve_max_candidates: int = field(
        default=8,
        metadata={"toml": "worker.conflict_auto_resolve_max_candidates"},
    )
    maintenance_judge_base_url: str = _toml_field("http://127.0.0.1:8090/v1", "maintenance_judge.base_url")
    maintenance_judge_model: str = _toml_field("Qwen3.8-27B-UD-IQ4_XS.gguf", "maintenance_judge.model")
    maintenance_judge_prompt_version: str = _toml_field("conflict-auto-v1", "maintenance_judge.prompt_version")
    maintenance_judge_tokenizer_identity: str = _toml_field(
        "qwen3.8-gguf-embedded", "maintenance_judge.tokenizer_identity"
    )
    maintenance_judge_timeout_seconds: float = _toml_field(90.0, "maintenance_judge.timeout_seconds")
    worker_job_lease_minutes: int = field(default=5, metadata={"toml": "worker.job_lease_minutes"})
    daily_token_limit: int = field(default=500000, metadata={"toml": "worker.daily_token_limit"})
    audit_retention_days: int = field(default=30, metadata={"toml": "worker.audit_retention_days"})
    retention_days: int = field(default=30, metadata={"toml": "retention.event_days"})
    consolidate_cron: str = field(default="03:30", metadata={"toml": "worker.consolidate_cron"})
    consolidate_batch_size: int = field(default=100, metadata={"toml": "worker.consolidate_batch_size"})
    consolidate_confidence: float = field(
        default=0.8,
        metadata={"toml": "worker.consolidate_confidence"},
    )
    dedup_enabled: bool = field(default=True, metadata={"toml": "dedup.enabled"})
    dedup_threshold: float = field(default=0.92, metadata={"toml": "dedup.threshold"})
    dedup_audit_only: bool = field(default=True, metadata={"toml": "dedup.audit_only"})
    dedup_auto_merge_min_confidence: float = field(
        default=0.98,
        metadata={"toml": "dedup.auto_merge_min_confidence"},
    )
    dedup_scan_limit: int = field(default=200, metadata={"toml": "dedup.scan_limit"})
    dedup_cron: str = field(default="03:00", metadata={"toml": "dedup.cron"})
    dedup_max_pending_pairs: int = field(
        default=10_000,
        metadata={"toml": "dedup.max_pending_pairs"},
    )
    induce_policies_cron: str = field(default="04:00", metadata={"toml": "worker.induce_policies_cron"})
    reclassify_cron: str = field(default="04:30", metadata={"toml": "worker.reclassify_cron"})
    memory_temporal_ttl_days: int = field(
        default=7,
        metadata={"toml": "retention.temporal_ttl_days"},
    )
    operational_cleanup_enabled: bool = field(
        default=True,
        metadata={"toml": "retention.operational_cleanup_enabled"},
    )
    operational_batch_size: int = field(
        default=2_000,
        metadata={"toml": "retention.operational_batch_size"},
    )
    expired_cleanup_mode: ExpiredCleanupMode = field(
        default="observe",
        metadata={"toml": "retention.expired_cleanup_mode"},
    )
    expired_claim_retention_days: int = field(
        default=90,
        metadata={"toml": "retention.expired_claim_retention_days"},
    )
    expired_cleanup_batch_size: int = field(
        default=100,
        metadata={"toml": "retention.expired_cleanup_batch_size"},
    )
    job_succeeded_days: int = field(default=30, metadata={"toml": "retention.job_succeeded_days"})
    job_dead_days: int = field(default=90, metadata={"toml": "retention.job_dead_days"})
    llm_span_days: int = field(default=30, metadata={"toml": "retention.llm_span_days"})
    dedup_pair_days: int = field(default=90, metadata={"toml": "retention.dedup_pair_days"})
    feedback_uninjected_days: int = field(
        default=7,
        metadata={"toml": "retention.feedback_uninjected_days"},
    )
    feedback_unlabeled_days: int = field(
        default=90,
        metadata={"toml": "retention.feedback_unlabeled_days"},
    )
    temporal_ttl_days_low: int = field(
        default=3,
        metadata={"toml": "retention.temporal_ttl_days_low"},
    )
    temporal_ttl_days_normal: int = field(
        default=7,
        metadata={"toml": "retention.temporal_ttl_days_normal"},
    )
    temporal_ttl_days_high: int = field(
        default=14,
        metadata={"toml": "retention.temporal_ttl_days_high"},
    )
    importance_low_threshold: float = field(
        default=0.4,
        metadata={"toml": "retention.importance_low_threshold"},
    )
    importance_high_threshold: float = field(
        default=0.7,
        metadata={"toml": "retention.importance_high_threshold"},
    )
    importance_write_floor: float = field(
        default=0.2,
        metadata={"toml": "retention.importance_write_floor"},
    )
    slot_short_ttl_seconds: int = field(
        default=86400,
        metadata={"toml": "retention.slot_short_ttl_seconds"},
    )
    ttl_backfill_batch_size: int = field(
        default=100,
        metadata={"toml": "retention.ttl_backfill_batch_size"},
    )
    ttl_backfill_grace_hours: int = field(
        default=0,
        metadata={"toml": "retention.ttl_backfill_grace_hours"},
    )
    temporal_cleanup_age_days: int = field(
        default=30,
        metadata={"toml": "retention.temporal_cleanup_age_days"},
    )
    temporal_cleanup_expiry_days: int = field(
        default=90,
        metadata={"toml": "retention.temporal_cleanup_expiry_days"},
    )
    decay_temporal_days: int = field(default=7, metadata={"toml": "retention.decay_temporal_days"})
    archive_temporal_days: int = field(default=30, metadata={"toml": "retention.archive_temporal_days"})
    decay_permanent_days: int = field(default=90, metadata={"toml": "retention.decay_permanent_days"})
    archive_permanent_days: int = field(
        default=180,
        metadata={"toml": "retention.archive_permanent_days"},
    )
    access_bonus_every: int = field(default=5, metadata={"toml": "retention.access_bonus_every"})
    access_bonus_days: int = field(default=1, metadata={"toml": "retention.access_bonus_days"})
    access_bonus_cap_days: int = field(default=30, metadata={"toml": "retention.access_bonus_cap_days"})
    decay_rollout_grace_days: int = field(
        default=7,
        metadata={"toml": "retention.decay_rollout_grace_days"},
    )
    decay_min_confidence: float = field(
        default=0.05,
        metadata={"toml": "retention.decay_min_confidence"},
    )
    decay_model: DecayModel = field(default="activation_halflife", metadata={"toml": "decay.model"})
    decay_temporal_half_life_days: int = field(
        default=45,
        metadata={"toml": "decay.temporal_half_life_days"},
    )
    decay_permanent_half_life_days: int = field(
        default=90,
        metadata={"toml": "decay.permanent_half_life_days"},
    )
    decay_identity_half_life_days: int = field(
        default=365,
        metadata={"toml": "decay.identity_half_life_days"},
    )
    decay_halflife_archive_threshold: float = field(
        default=0.05,
        metadata={"toml": "decay.halflife_archive_threshold"},
    )
    decay_halflife_archive_grace_days: int = field(
        default=7,
        metadata={"toml": "decay.halflife_archive_grace_days"},
    )
    # feedback_lifecycle: observe 只聚合 usefulness，不影响 TTL/decay；观察稳定后可切换为 on
    feedback_lifecycle_mode: FeedbackLifecycleMode = field(
        default="observe",
        metadata={"toml": "retention.feedback_lifecycle_mode"},
    )
    feedback_bonus_every: int = field(default=3, metadata={"toml": "retention.feedback_bonus_every"})
    feedback_bonus_days: int = field(default=14, metadata={"toml": "retention.feedback_bonus_days"})
    feedback_bonus_cap_days: int = field(
        default=180,
        metadata={"toml": "retention.feedback_bonus_cap_days"},
    )
    feedback_min_samples: int = field(default=3, metadata={"toml": "recall.feedback_min_samples"})
    max_request_body: int = field(default=2 * 1024 * 1024, metadata={"toml": "server.max_request_body"})
    alert_webhook_url: str | None = field(default=None, metadata={"toml": "alert.webhook_url"})
    alert_dedupe_seconds: float = field(default=300.0, metadata={"toml": "alert.dedupe_seconds"})
    expansion_circuit_failure_threshold: int = field(
        default=5,
        metadata={"toml": "recall.expansion_circuit_failure_threshold"},
    )
    expansion_circuit_open_seconds: float = field(
        default=60.0,
        metadata={"toml": "recall.expansion_circuit_open_seconds"},
    )
    smtp_host: str | None = field(default=None, metadata={"toml": "alert.smtp_host"})
    smtp_port: int = field(default=25, metadata={"toml": "alert.smtp_port"})
    alert_email_from: str | None = field(default=None, metadata={"toml": "alert.email_from"})
    alert_email_to: str | None = field(default=None, metadata={"toml": "alert.email_to"})

    @classmethod
    def for_test(cls) -> "Settings":
        """返回不创建真实网络客户端的显式测试配置。"""
        return cls(
            embedder_mode="fake",
            extractor_mode="fake",
            reranker_mode="off",
            query_expansion_mode="off",
            relation_discovery_mode="off",
            image_describer_mode="off",
        )

    def validate(self) -> None:
        """校验配置组合以及已启用组件的密钥。"""
        if self.fts_language not in {"auto", "zh", "en"}:
            raise ConfigurationError("recall.fts_language must be 'auto', 'zh', or 'en'")
        if self.resurrection_mode not in {"off", "auto"}:
            raise ConfigurationError("recall.resurrection_mode must be 'off' or 'auto'")
        if self.resurrection_candidate_limit < 1:
            raise ConfigurationError("recall.resurrection_candidate_limit must be positive")
        if not 0.0 < self.resurrection_min_term_coverage <= 1.0:
            raise ConfigurationError("recall.resurrection_min_term_coverage must be between 0 and 1")
        try:
            VectorBackend(self.vector_backend)
        except ValueError as error:
            raise ConfigurationError("recall.vector_backend must be 'sqlite_scan' or 'sqlite_vec'") from error
        required_secrets: dict[str, tuple[str | None, list[str]]] = {}
        llm_disable_modes: list[str] = []
        if self.extractor_mode != "fake":
            llm_disable_modes.append("extraction.mode='fake'")
        if self.query_expansion_mode != "off":
            llm_disable_modes.append("recall.query_expansion_mode='off'")
        if self.relation_discovery_mode != "off":
            llm_disable_modes.append("relation.discovery_mode='off'")
        if llm_disable_modes:
            required_secrets["LLM_API_KEY"] = (self.llm_api_key, llm_disable_modes)
        if self.embedder_mode == "real":
            required_secrets["EMBEDDING_API_KEY"] = (self.embedding_api_key, ["embedding.mode='fake'"])
        if self.reranker_mode in {"on", "real"}:
            required_secrets["RERANKER_API_KEY"] = (self.reranker_api_key, ["reranker.mode='off'"])
        if self.image_describer_mode == "on":
            required_secrets["IMAGE_API_KEY"] = (self.image_describer_api_key, ["image_describer.mode='off'"])
        invalid_secrets = [
            name for name, (value, _disable_modes) in required_secrets.items() if is_placeholder_secret(value)
        ]
        if invalid_secrets:
            recovery = "; ".join(f"{name} -> {', '.join(required_secrets[name][1])}" for name in invalid_secrets)
            raise ConfigurationError(
                "missing or placeholder API key(s) for enabled component(s): "
                f"{', '.join(invalid_secrets)}; add each key to .env or disable its TOML mode: {recovery}"
            )
        if self.database_pool_size < 1 or self.database_busy_timeout_seconds < 1:
            raise ConfigurationError("database pool size and busy timeout must be positive")
        if self.entity_aliases_path is not None and not self.entity_aliases_path.strip():
            raise ConfigurationError("entity aliases path must not be empty")
        if self.price_target_mode not in {"off", "audit", "observe", "enforce"}:
            raise ConfigurationError("price.target_mode must be 'off', 'audit', 'observe', or 'enforce'")
        if self.plan_fulfillment_mode not in {"off", "audit", "observe", "enforce"}:
            raise ConfigurationError("plan.fulfillment_mode must be 'off', 'audit', 'observe', or 'enforce'")
        if self.recall_default_limit < 1 or self.recall_default_limit > 100:
            raise ConfigurationError("recall default limit must be between 1 and 100")
        if self.recall_vector_scan_limit < 1:
            raise ConfigurationError("recall vector scan limit must be positive")
        if not isinstance(self.recall_dense_enabled, bool):
            raise ConfigurationError("recall dense enabled must be a boolean")
        if not isinstance(self.hermes_enabled, bool) or not isinstance(self.hermes_manual_conflict_notice, bool):
            raise ConfigurationError("hermes enabled and manual conflict notice must be booleans")
        if self.hermes_timeout < 1:
            raise ConfigurationError("hermes timeout must be positive")
        if self.hermes_on_demand_recall_timeout_seconds <= 0:
            raise ConfigurationError("hermes.on_demand_recall_timeout_seconds must be positive")
        if self.hermes_enabled and not self.hermes_url.strip():
            raise ConfigurationError("hermes URL must not be empty when Hermes is enabled")
        if self.hermes_home is not None and not self.hermes_home.strip():
            raise ConfigurationError("hermes home must not be empty")
        if not self.llm_model.strip() or self.llm_timeout <= 0:
            raise ConfigurationError("LLM model must not be empty and timeout must be positive")
        if (
            min(
                self.decay_temporal_days,
                self.archive_temporal_days,
                self.decay_permanent_days,
                self.archive_permanent_days,
                self.access_bonus_every,
                self.decay_rollout_grace_days,
            )
            < 1
        ):
            raise ConfigurationError("decay, archive, and access bonus intervals must be positive")
        if min(self.access_bonus_days, self.access_bonus_cap_days) < 0:
            raise ConfigurationError("access bonus days and cap must be non-negative")
        if not 0.0 <= self.decay_min_confidence <= 1.0:
            raise ConfigurationError("decay minimum confidence must be between 0 and 1")
        if self.decay_model not in {"legacy_linear", "activation_halflife", "confidence_halflife"}:
            raise ConfigurationError(
                "decay.model must be 'legacy_linear', 'activation_halflife', or 'confidence_halflife'"
            )
        if (
            min(
                self.decay_temporal_half_life_days,
                self.decay_permanent_half_life_days,
                self.decay_identity_half_life_days,
                self.decay_halflife_archive_grace_days,
            )
            < 1
        ):
            raise ConfigurationError("decay half-life and archive grace days must be positive")
        if not 0.0 < self.decay_halflife_archive_threshold < 1.0:
            raise ConfigurationError("decay.halflife_archive_threshold must be between 0 and 1")
        if self.decay_temporal_days > self.archive_temporal_days:
            raise ConfigurationError("temporal decay days must not exceed archive days")
        if self.decay_permanent_days > self.archive_permanent_days:
            raise ConfigurationError("permanent decay days must not exceed archive days")
        if self.feedback_lifecycle_mode not in {"off", "observe", "on"}:
            raise ConfigurationError("retention.feedback_lifecycle_mode must be 'off', 'observe', or 'on'")
        if self.expired_cleanup_mode not in {"off", "observe", "on"}:
            raise ConfigurationError("retention.expired_cleanup_mode must be 'off', 'observe', or 'on'")
        if self.feedback_bonus_every <= 0:
            raise ConfigurationError("retention.feedback_bonus_every must be positive")
        if min(self.feedback_bonus_days, self.feedback_bonus_cap_days) < 0:
            raise ConfigurationError("feedback bonus days and cap must be non-negative")
        if self.feedback_min_samples <= 0:
            raise ConfigurationError("recall.feedback_min_samples must be positive")
        if self.llm_provider not in {"dashscope", "zhipu", "openai_compatible"}:
            raise ConfigurationError("llm.provider must be 'dashscope', 'zhipu', or 'openai_compatible'")
        if self.llm_structured_mode not in {"auto", "json_object", "json_schema"}:
            raise ConfigurationError("llm.structured_mode must be 'auto', 'json_object', or 'json_schema'")
        if not isinstance(self.enable_llm_thinking, bool):
            raise ConfigurationError("llm.enable_thinking must be a boolean")
        if self.relation_expansion_mode not in {"off", "on"}:
            raise ConfigurationError("relation.expansion_mode must be 'off' or 'on'")
        if self.relation_expansion_max_depth < 1:
            raise ConfigurationError("relation.expansion_max_depth must be at least 1")
        if self.relation_discovery_mode not in {"off", "audit", "auto"}:
            raise ConfigurationError("relation.discovery_mode must be 'off', 'audit', or 'auto'")
        if self.relation_discovery_pool_limit < 1 or self.relation_discovery_max_proposals < 1:
            raise ConfigurationError("relation discovery limits must be positive")
        if not 0.0 <= self.relation_auto_apply_confidence <= 1.0:
            raise ConfigurationError("relation.auto_apply_confidence must be between 0 and 1")
        if not 0.0 <= self.relation_conflict_confidence <= 1.0:
            raise ConfigurationError("relation.conflict_confidence must be between 0 and 1")
        if self.packed_context_token_budget < 1 or self.recall_candidate_floor < 1:
            raise ConfigurationError("recall budgets must be positive")
        if not 0.0 <= self.recall_dedup_threshold <= 1.0:
            raise ConfigurationError("recall.dedup_threshold must be between 0 and 1 (0 disables fold)")
        if self.recall_dedup_candidate_limit < 1:
            raise ConfigurationError("recall.dedup_candidate_limit must be positive")
        if self.echo_suppression_mode not in {"off", "observe", "enforce"}:
            raise ConfigurationError("recall.echo_suppression_mode must be 'off', 'observe', or 'enforce'")
        if not 60 <= self.echo_session_window_seconds <= 14_400:
            raise ConfigurationError("recall.echo_session_window_seconds must be between 60 and 14400")
        if not isinstance(self.echo_pending_review_enabled, bool):
            raise ConfigurationError("recall.echo_pending_review_enabled must be a boolean")
        if not 0.0 <= self.echo_pending_similarity_threshold <= 1.0:
            raise ConfigurationError("recall.echo_pending_similarity_threshold must be between 0 and 1")
        if self.echo_pending_max_seconds < 60:
            raise ConfigurationError("recall.echo_pending_max_seconds must be at least 60")
        if self.freshness_annotation_mode not in {"off", "observe", "render"}:
            raise ConfigurationError("recall.freshness_annotation_mode must be 'off', 'observe', or 'render'")
        if self.relevance_gate_mode not in {"off", "observe", "enforce"}:
            raise ConfigurationError("recall.relevance_gate_mode must be 'off', 'observe', or 'enforce'")
        relevance_thresholds = {
            "recall.relevance_reranker_floor": self.relevance_reranker_floor,
            "recall.relevance_dense_floor": self.relevance_dense_floor,
            "recall.relevance_relative_drop": self.relevance_relative_drop,
        }
        for variable_name, value in relevance_thresholds.items():
            if not 0.0 <= value <= 1.0:
                raise ConfigurationError(f"{variable_name} must be between 0 and 1")
        if not isinstance(self.relevance_keep_top1, bool):
            raise ConfigurationError("recall.relevance_keep_top1 must be a boolean")
        allowed_relevance_intents = {
            "current_state",
            "preference",
            "historical",
            "tool",
            "procedure",
        }
        if not self.relevance_intents or any(
            intent not in allowed_relevance_intents for intent in self.relevance_intents
        ):
            raise ConfigurationError("recall.relevance_intents must contain valid recall intents")
        if not 0.0 <= self.preference_recency_boost <= 1.0:
            raise ConfigurationError("recall.preference_recency_boost must be between 0 and 1")
        if not 0.0 <= self.tag_boost_weight <= 1.0:
            raise ConfigurationError("recall.tag_boost_weight must be between 0 and 1")
        if not 0.0 <= self.tag_channel_weight <= 1.0:
            raise ConfigurationError("recall.tag_channel_weight must be between 0 and 1")
        if self.tag_candidate_limit < 1:
            raise ConfigurationError("recall.tag_candidate_limit must be positive")
        if self.query_expansion_mode not in {"off", "auto", "always"}:
            raise ConfigurationError("recall.query_expansion_mode must be 'off', 'auto', or 'always'")
        if self.query_context_mode not in {"off", "coreference"}:
            raise ConfigurationError("recall.query_context_mode must be 'off' or 'coreference'")
        if not 0 <= self.query_expansion_max <= 2:
            raise ConfigurationError("recall.query_expansion_max must be between 0 and 2")
        if (
            min(
                self.query_expansion_candidate_floor,
                self.query_expansion_token_ceiling,
                self.query_expansion_timeout_seconds,
                self.query_expansion_total_timeout_seconds,
                self.query_expansion_max_concurrency,
                self.query_context_max_events,
                self.query_context_token_budget,
            )
            <= 0
        ):
            raise ConfigurationError("query expansion budgets and timeouts must be positive")
        if self.procedure_recall_mode not in {"off", "keyword", "auto"}:
            raise ConfigurationError("recall.procedure_mode must be 'off', 'keyword', or 'auto'")
        if not 0.0 <= self.procedure_llm_threshold <= 1.0:
            raise ConfigurationError("recall.procedure_llm_threshold must be between 0 and 1")
        if (
            min(
                self.procedure_router_timeout_seconds,
                self.procedure_candidate_limit,
                self.procedure_recent_outcome_window,
                self.procedure_outcome_half_life_days,
            )
            <= 0
        ):
            raise ConfigurationError("procedure recall limits and timeouts must be positive")
        if self.recall_side_effect_max_attempts < 1 or self.recall_side_effect_backoff_seconds < 0:
            raise ConfigurationError("recall side-effect attempts must be positive and backoff non-negative")
        if self.vector_batch_size < 1:
            raise ConfigurationError("recall.vector_batch_size must be positive")
        if (
            self.hermes_circuit_failure_threshold < 1
            or self.hermes_circuit_open_seconds <= 0
            or self.hermes_prefetch_cache_ttl_seconds <= 0
        ):
            raise ConfigurationError("Hermes circuit breaker and prefetch cache values must be positive")
        if self.policy_induction_lookback_days < 1 or self.policy_induction_min_episodes < 1:
            raise ConfigurationError("policy induction values must be positive")
        if self.llm_max_attempts < 1:
            raise ConfigurationError("llm.max_attempts must be at least 1")
        if self.llm_schema_retries < 0:
            raise ConfigurationError("llm.schema_retries must be non-negative")
        if self.image_describer_mode not in {"off", "on"}:
            raise ConfigurationError("image_describer.mode must be 'off' or 'on'")
        if self.image_describer_provider != "dashscope":
            raise ConfigurationError("image_describer.provider must be 'dashscope'")
        if self.image_max_bytes < 1 or self.image_max_parts < 1 or self.image_describer_timeout_seconds <= 0:
            raise ConfigurationError("image limits and timeout must be positive")
        if self.image_describer_mode == "on":
            if not self.image_describer_base_url.lower().startswith("https://"):
                raise ConfigurationError("image_describer.base_url must use HTTPS")
            if not self.image_describer_model.strip():
                raise ConfigurationError("image_describer.model must not be empty")
            if self.image_allow_file_uris and not self.image_file_allow_roots:
                raise ConfigurationError(
                    "image_describer.file_allow_roots is required when file image URIs are enabled"
                )
        if not 0.0 <= self.dedup_threshold <= 1.0:
            raise ConfigurationError("dedup.threshold must be between 0 and 1")
        if not self.dedup_threshold <= self.dedup_auto_merge_min_confidence <= 1.0:
            raise ConfigurationError("dedup.auto_merge_min_confidence must be between dedup.threshold and 1")
        if self.dedup_scan_limit < 1:
            raise ConfigurationError("dedup.scan_limit must be positive")
        if self.dedup_max_pending_pairs < 1:
            raise ConfigurationError("dedup.max_pending_pairs must be positive")
        parse_daily_cron(self.dedup_cron, "dedup.cron")
        if self.extraction_chunk_target_chars < 1:
            raise ConfigurationError("extraction.chunk_target_chars must be positive")
        if self.extraction_chunk_overlap_turns < 0:
            raise ConfigurationError("extraction.chunk_overlap_turns must be non-negative")
        if self.extraction_max_split_depth < 0:
            raise ConfigurationError("extraction.max_split_depth must be non-negative")
        if not 1 <= self.extraction_batch_max_events <= 32:
            raise ConfigurationError("extraction.batch_max_events must be between 1 and 32")
        if self.extraction_batch_max_wait_seconds < 0:
            raise ConfigurationError("extraction.batch_max_wait_seconds must be non-negative")
        if self.worker_job_lease_minutes < 1:
            raise ConfigurationError("worker.job_lease_minutes must be positive")
        if not 1 <= self.conflict_maintenance_max_cases <= 1_000:
            raise ConfigurationError("worker.conflict_maintenance_max_cases must be between 1 and 1000")
        _validate_runtime_modes(self)
        if not 50 <= self.conflict_maintenance_budget_ms <= 10_000:
            raise ConfigurationError("worker.conflict_maintenance_budget_ms must be between 50 and 10000")
        if not 1 <= self.conflict_failure_backoff_seconds <= 86_400:
            raise ConfigurationError("worker.conflict_failure_backoff_seconds must be between 1 and 86400")
        if not 0 <= self.conflict_writer_yield_ms <= 1_000:
            raise ConfigurationError("worker.conflict_writer_yield_ms must be between 0 and 1000")
        if not 2 <= self.conflict_auto_resolve_max_candidates <= 10_000:
            raise ConfigurationError("worker.conflict_auto_resolve_max_candidates must be between 2 and 10000")
        if (
            min(
                self.operational_batch_size,
                self.job_succeeded_days,
                self.job_dead_days,
                self.llm_span_days,
                self.dedup_pair_days,
                self.feedback_uninjected_days,
                self.feedback_unlabeled_days,
            )
            < 1
        ):
            raise ConfigurationError(
                "retention.operational_batch_size, retention.job_succeeded_days, "
                "retention.job_dead_days, retention.llm_span_days, retention.dedup_pair_days, "
                "retention.feedback_uninjected_days, and retention.feedback_unlabeled_days must be positive"
            )
        if self.expired_claim_retention_days < 1:
            raise ConfigurationError("retention.expired_claim_retention_days must be positive")
        if self.expired_cleanup_batch_size < 1:
            raise ConfigurationError("retention.expired_cleanup_batch_size must be positive")
        if self.verification_mode not in {"off", "audit", "enforce"}:
            raise ConfigurationError("extraction.verification_mode must be 'off', 'audit', or 'enforce'")
        if (
            min(
                self.temporal_ttl_days_low,
                self.temporal_ttl_days_normal,
                self.temporal_ttl_days_high,
                self.slot_short_ttl_seconds,
            )
            < 1
        ):
            raise ConfigurationError("TTL durations must be positive")
        if self.ttl_backfill_batch_size < 1 or self.ttl_backfill_grace_hours < 0:
            raise ConfigurationError("TTL backfill batch size must be positive and grace hours non-negative")
        if min(self.temporal_cleanup_age_days, self.temporal_cleanup_expiry_days) < 1:
            raise ConfigurationError("temporal cleanup durations must be positive")
        if not (
            0.0 <= self.importance_write_floor <= self.importance_low_threshold <= self.importance_high_threshold <= 1.0
        ):
            raise ConfigurationError("importance thresholds must be ordered between 0 and 1")
        if self.embedder_mode not in {"fake", "real"}:
            raise ConfigurationError("embedding.mode must be 'fake' or 'real'")
        if self.embedding_api_mode not in {"compatible", "native"}:
            raise ConfigurationError("embedding.api_mode must be 'compatible' or 'native'")
        if self.embedding_text_type not in {None, "", "document", "query"}:
            raise ConfigurationError("embedding.text_type must be '', 'document', or 'query'")
        if self.index_text_mode not in {"legacy", "value_only", "natural", "answerable"}:
            raise ConfigurationError("index.text_mode must be 'legacy', 'value_only', 'natural', or 'answerable'")
        if self.index_backfill_batch_size < 1 or self.index_backfill_max_attempts < 1:
            raise ConfigurationError("index backfill batch size and max attempts must be positive")
        if not self.index_text_version.strip():
            raise ConfigurationError("index.text_version must not be empty")
        if self.reranker_mode not in {"off", "fake", "on", "real"}:
            raise ConfigurationError("reranker.mode must be 'off', 'fake', 'on', or 'real'")
        if self.reranker_provider != "dashscope":
            raise ConfigurationError("reranker.provider must be 'dashscope'")
        if self.extractor_mode not in {"fake", "real", "llm"}:
            raise ConfigurationError("extraction.mode must be 'fake', 'real', or 'llm'")

    def _validate(self) -> None:
        """兼容旧调用方，委托公开配置校验入口。"""
        self.validate()

    def snapshot(self) -> dict[str, Any]:
        """返回可用于健康检查和审计的非敏感配置。"""
        return {
            "embedder_mode": self.embedder_mode,
            "embedding_dim": self.embedding_dim,
            "embedding_api_mode": self.embedding_api_mode,
            "embedding_text_type": self.embedding_text_type,
            "price_target_mode": self.price_target_mode,
            "plan_fulfillment_mode": self.plan_fulfillment_mode,
            "latest_wins_mode": self.latest_wins_mode,
            "latest_wins_slots": self.latest_wins_slots,
            "index_text_mode": self.index_text_mode,
            "index_backfill_batch_size": self.index_backfill_batch_size,
            "index_backfill_max_attempts": self.index_backfill_max_attempts,
            "index_text_version": self.index_text_version,
            "reranker_mode": self.reranker_mode,
            "reranker_provider": self.reranker_provider,
            "relevance_gate_mode": self.relevance_gate_mode,
            "relevance_reranker_floor": self.relevance_reranker_floor,
            "relevance_dense_floor": self.relevance_dense_floor,
            "relevance_relative_drop": self.relevance_relative_drop,
            "relevance_keep_top1": self.relevance_keep_top1,
            "relevance_intents": list(self.relevance_intents),
            "relation_expansion_mode": self.relation_expansion_mode,
            "relation_expansion_max_depth": self.relation_expansion_max_depth,
            "relation_discovery_mode": self.relation_discovery_mode,
            "relation_discovery_pool_limit": self.relation_discovery_pool_limit,
            "relation_discovery_max_proposals": self.relation_discovery_max_proposals,
            "recall_default_limit": self.recall_default_limit,
            "fts_language": self.fts_language,
            "recall_vector_scan_limit": self.recall_vector_scan_limit,
            "recall_dense_enabled": self.recall_dense_enabled,
            "entity_constraint_mode": self.entity_constraint_mode,
            "echo_suppression_mode": self.echo_suppression_mode,
            "echo_session_window_seconds": self.echo_session_window_seconds,
            "echo_pending_review_enabled": self.echo_pending_review_enabled,
            "echo_pending_similarity_threshold": self.echo_pending_similarity_threshold,
            "echo_pending_max_seconds": self.echo_pending_max_seconds,
            "freshness_annotation_mode": self.freshness_annotation_mode,
            "resurrection_mode": self.resurrection_mode,
            "resurrection_candidate_limit": self.resurrection_candidate_limit,
            "resurrection_min_term_coverage": self.resurrection_min_term_coverage,
            "tag_boost_enabled": self.tag_boost_enabled,
            "tag_boost_weight": self.tag_boost_weight,
            "tag_channel_enabled": self.tag_channel_enabled,
            "tag_channel_weight": self.tag_channel_weight,
            "tag_candidate_limit": self.tag_candidate_limit,
            "query_expansion_mode": self.query_expansion_mode,
            "query_expansion_model": self.query_expansion_model,
            "query_expansion_max": self.query_expansion_max,
            "query_expansion_candidate_floor": self.query_expansion_candidate_floor,
            "query_expansion_token_ceiling": self.query_expansion_token_ceiling,
            "query_expansion_timeout_seconds": self.query_expansion_timeout_seconds,
            "query_expansion_total_timeout_seconds": self.query_expansion_total_timeout_seconds,
            "query_expansion_max_concurrency": self.query_expansion_max_concurrency,
            "query_context_mode": self.query_context_mode,
            "query_context_max_events": self.query_context_max_events,
            "query_context_token_budget": self.query_context_token_budget,
            "procedure_recall_mode": self.procedure_recall_mode,
            "procedure_llm_threshold": self.procedure_llm_threshold,
            "procedure_router_timeout_seconds": self.procedure_router_timeout_seconds,
            "procedure_candidate_limit": self.procedure_candidate_limit,
            "procedure_recent_outcome_window": self.procedure_recent_outcome_window,
            "procedure_outcome_half_life_days": self.procedure_outcome_half_life_days,
            "decay_model": self.decay_model,
            "decay_temporal_half_life_days": self.decay_temporal_half_life_days,
            "decay_permanent_half_life_days": self.decay_permanent_half_life_days,
            "decay_identity_half_life_days": self.decay_identity_half_life_days,
            "decay_halflife_archive_threshold": self.decay_halflife_archive_threshold,
            "decay_halflife_archive_grace_days": self.decay_halflife_archive_grace_days,
            "recall_side_effect_max_attempts": self.recall_side_effect_max_attempts,
            "recall_side_effect_backoff_seconds": self.recall_side_effect_backoff_seconds,
            "vector_backend": self.vector_backend,
            "vector_batch_size": self.vector_batch_size,
            "hermes_on_demand_recall_timeout_seconds": self.hermes_on_demand_recall_timeout_seconds,
            "hermes_manual_conflict_notice": self.hermes_manual_conflict_notice,
            "extract_pre_filter": self.extract_pre_filter,
            "verification_mode": self.verification_mode,
            "lesson_signal_mode": self.lesson_signal_mode,
            "llm_model": self.llm_model,
            "llm_provider": self.llm_provider,
            "llm_structured_mode": self.llm_structured_mode,
            "enable_llm_thinking": self.enable_llm_thinking,
            "image_describer_mode": self.image_describer_mode,
            "image_describer_provider": self.image_describer_provider,
            "image_describer_model": self.image_describer_model,
            "extraction_chunk_target_chars": self.extraction_chunk_target_chars,
            "extraction_chunk_overlap_turns": self.extraction_chunk_overlap_turns,
            "extraction_max_split_depth": self.extraction_max_split_depth,
            "extraction_soft_split_enabled": self.extraction_soft_split_enabled,
            "extraction_batch_max_events": self.extraction_batch_max_events,
            "extraction_batch_max_wait_seconds": self.extraction_batch_max_wait_seconds,
            "conflict_auto_resolve_enabled": self.conflict_auto_resolve_enabled,
            "conflict_auto_mode": self.conflict_auto_mode,
            "conflict_l1_min_time_delta_seconds": self.conflict_l1_min_time_delta_seconds,
            "conflict_l1_min_confidence_delta": self.conflict_l1_min_confidence_delta,
            "conflict_maintenance_max_cases": self.conflict_maintenance_max_cases,
            "conflict_maintenance_budget_ms": self.conflict_maintenance_budget_ms,
            "conflict_failure_backoff_seconds": self.conflict_failure_backoff_seconds,
            "conflict_writer_yield_ms": self.conflict_writer_yield_ms,
            "conflict_auto_resolve_max_candidates": self.conflict_auto_resolve_max_candidates,
            "maintenance_judge_base_url": self.maintenance_judge_base_url,
            "maintenance_judge_model": self.maintenance_judge_model,
            "maintenance_judge_prompt_version": self.maintenance_judge_prompt_version,
            "maintenance_judge_tokenizer_identity": self.maintenance_judge_tokenizer_identity,
            "maintenance_judge_timeout_seconds": self.maintenance_judge_timeout_seconds,
            "operational_cleanup_enabled": self.operational_cleanup_enabled,
            "operational_batch_size": self.operational_batch_size,
            "job_succeeded_days": self.job_succeeded_days,
            "job_dead_days": self.job_dead_days,
            "llm_span_days": self.llm_span_days,
            "dedup_pair_days": self.dedup_pair_days,
            "feedback_uninjected_days": self.feedback_uninjected_days,
            "feedback_unlabeled_days": self.feedback_unlabeled_days,
            "dedup_max_pending_pairs": self.dedup_max_pending_pairs,
        }

    def retention_policy(self) -> TTLPolicy:
        """构造不依赖基础设施的 Claim TTL 策略。"""
        return TTLPolicy(
            temporal_ttl_days_low=self.temporal_ttl_days_low,
            temporal_ttl_days_normal=self.temporal_ttl_days_normal,
            temporal_ttl_days_high=self.temporal_ttl_days_high,
            importance_low_threshold=self.importance_low_threshold,
            importance_high_threshold=self.importance_high_threshold,
            importance_write_floor=self.importance_write_floor,
            slot_short_ttl_seconds=self.slot_short_ttl_seconds,
        )
