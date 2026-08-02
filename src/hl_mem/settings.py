"""集中化配置入口：启动时解析一次并校验配置组合。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from hl_mem.domain.claims.retention import TTLPolicy
from hl_mem.errors import ConfigurationError

EmbedderMode = Literal["fake", "real"]
RerankerMode = Literal["off", "fake", "on", "real"]
RerankerProvider = Literal["dashscope"]
RelationExpansionMode = Literal["off", "on"]
RelationDiscoveryMode = Literal["off", "audit", "auto"]
ExtractorMode = Literal["fake", "real", "llm"]
LLMProvider = Literal["dashscope", "zhipu", "openai_compatible"]
StructuredOutputModeName = Literal["auto", "json_object", "json_schema"]
QueryExpansionMode = Literal["off", "auto", "always"]
QueryContextMode = Literal["off", "coreference"]
ProcedureRecallMode = Literal["off", "keyword", "auto"]
FeedbackLifecycleMode = Literal["off", "observe", "on"]
RelevanceGateMode = Literal["off", "observe", "enforce"]
ImageDescriberMode = Literal["off", "on"]
ImageDescriberProvider = Literal["dashscope"]
IndexTextMode = Literal["legacy", "value_only", "natural", "answerable"]


class VectorBackend(StrEnum):
    """支持的向量检索后端。"""

    SQLITE_SCAN = "sqlite_scan"


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
    embedding_connect_timeout: float = field(
        default=5.0,
        metadata={"toml": "embedding.connect_timeout"},
    )
    embedding_read_timeout: float = field(default=30.0, metadata={"toml": "embedding.read_timeout"})
    embedding_max_attempts: int = field(default=3, metadata={"toml": "embedding.max_attempts"})
    index_text_mode: IndexTextMode = field(default="legacy", metadata={"toml": "index.text_mode"})
    index_backfill_batch_size: int = field(default=100, metadata={"toml": "index.backfill_batch_size"})
    index_backfill_max_attempts: int = field(
        default=3,
        metadata={"toml": "index.backfill_max_attempts"},
    )
    index_text_version: str = field(default="v1", metadata={"toml": "index.text_version"})
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
    reranker_model: str = field(default="gte-rerank-v2", metadata={"toml": "reranker.model"})
    relation_expansion_mode: RelationExpansionMode = field(
        default="off",
        metadata={"toml": "relation.expansion_mode"},
    )
    relation_expansion_max_depth: int = field(
        default=1,
        metadata={"toml": "relation.expansion_max_depth"},
    )
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
    recall_default_limit: int = field(default=20, metadata={"toml": "recall.default_limit"})
    recall_vector_scan_limit: int = field(default=200, metadata={"toml": "recall.vector_scan_limit"})
    packed_context_token_budget: int = field(
        default=2000,
        metadata={"toml": "recall.packed_context_token_budget"},
    )
    recall_candidate_floor: int = field(default=50, metadata={"toml": "recall.candidate_floor"})
    recall_dedup_threshold: float = field(default=0.95, metadata={"toml": "recall.dedup_threshold"})
    recall_dedup_candidate_limit: int = field(
        default=100,
        metadata={"toml": "recall.dedup_candidate_limit"},
    )
    relevance_gate_mode: RelevanceGateMode = field(
        default="off",
        metadata={"toml": "recall.relevance_gate_mode"},
    )
    relevance_reranker_floor: float = field(
        default=0.4,
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
    # query_expansion: auto 仅在短查询或指代查询时触发 LLM 改写，提升 recall
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
    hermes_home: str | None = field(default=None, metadata={"toml": "hermes.home"})
    hermes_circuit_failure_threshold: int = field(
        default=5,
        metadata={"toml": "hermes.circuit_failure_threshold"},
    )
    hermes_circuit_open_seconds: float = field(
        default=60.0,
        metadata={"toml": "hermes.circuit_open_seconds"},
    )
    hermes_prefetch_cache_ttl_seconds: float = field(
        default=300.0,
        metadata={"toml": "hermes.prefetch_cache_ttl_seconds"},
    )
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
    extraction_chunk_target_chars: int = field(
        default=12000,
        metadata={"toml": "extraction.chunk_target_chars"},
    )
    extraction_chunk_overlap_turns: int = field(
        default=2,
        metadata={"toml": "extraction.chunk_overlap_turns"},
    )
    extraction_max_split_depth: int = field(
        default=3,
        metadata={"toml": "extraction.max_split_depth"},
    )
    worker_poll_interval: float = field(default=2.0, metadata={"toml": "worker.poll_interval"})
    worker_maintenance_interval: float = field(
        default=600.0,
        metadata={"toml": "worker.maintenance_interval"},
    )
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
    induce_policies_cron: str = field(default="04:00", metadata={"toml": "worker.induce_policies_cron"})
    reclassify_cron: str = field(default="04:30", metadata={"toml": "worker.reclassify_cron"})
    memory_temporal_ttl_days: int = field(
        default=7,
        metadata={"toml": "retention.temporal_ttl_days"},
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
        required_secrets: dict[str, str | None] = {}
        if self.extractor_mode != "fake" or self.query_expansion_mode != "off" or self.relation_discovery_mode != "off":
            required_secrets["LLM_API_KEY"] = self.llm_api_key
        if self.embedder_mode == "real":
            required_secrets["EMBEDDING_API_KEY"] = self.embedding_api_key
        if self.reranker_mode in {"on", "real"}:
            required_secrets["RERANKER_API_KEY"] = self.reranker_api_key
        if self.image_describer_mode == "on":
            required_secrets["IMAGE_API_KEY"] = self.image_describer_api_key
        invalid_secrets = [name for name, value in required_secrets.items() if is_placeholder_secret(value)]
        if invalid_secrets:
            raise ConfigurationError(
                "placeholder or empty secret configured for enabled component(s): "
                f"{', '.join(invalid_secrets)}; replace each value with a real API key"
            )
        if self.database_pool_size < 1 or self.database_busy_timeout_seconds < 1:
            raise ConfigurationError("database pool size and busy timeout must be positive")
        if self.entity_aliases_path is not None and not self.entity_aliases_path.strip():
            raise ConfigurationError("entity aliases path must not be empty")
        if self.recall_default_limit < 1 or self.recall_default_limit > 100:
            raise ConfigurationError("recall default limit must be between 1 and 100")
        if self.recall_vector_scan_limit < 1:
            raise ConfigurationError("recall vector scan limit must be positive")
        if not isinstance(self.hermes_enabled, bool):
            raise ConfigurationError("hermes enabled must be a boolean")
        if self.hermes_timeout < 1:
            raise ConfigurationError("hermes timeout must be positive")
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
        if self.decay_temporal_days > self.archive_temporal_days:
            raise ConfigurationError("temporal decay days must not exceed archive days")
        if self.decay_permanent_days > self.archive_permanent_days:
            raise ConfigurationError("permanent decay days must not exceed archive days")
        if self.feedback_lifecycle_mode not in {"off", "observe", "on"}:
            raise ConfigurationError("HL_MEM_FEEDBACK_LIFECYCLE_MODE must be 'off', 'observe', or 'on'")
        if self.feedback_bonus_every <= 0:
            raise ConfigurationError("HL_MEM_FEEDBACK_BONUS_EVERY must be positive")
        if min(self.feedback_bonus_days, self.feedback_bonus_cap_days) < 0:
            raise ConfigurationError("feedback bonus days and cap must be non-negative")
        if self.feedback_min_samples <= 0:
            raise ConfigurationError("HL_MEM_FEEDBACK_MIN_SAMPLES must be positive")
        if self.llm_provider not in {"dashscope", "zhipu", "openai_compatible"}:
            raise ConfigurationError("HL_MEM_LLM_PROVIDER must be 'dashscope', 'zhipu', or 'openai_compatible'")
        if self.llm_structured_mode not in {"auto", "json_object", "json_schema"}:
            raise ConfigurationError("HL_MEM_LLM_STRUCTURED_MODE must be 'auto', 'json_object', or 'json_schema'")
        if not isinstance(self.enable_llm_thinking, bool):
            raise ConfigurationError("HL_MEM_LLM_ENABLE_THINKING must be a boolean")
        if self.relation_expansion_mode not in {"off", "on"}:
            raise ConfigurationError("HL_MEM_RELATION_EXPANSION must be 'off' or 'on'")
        if self.relation_expansion_max_depth < 1:
            raise ConfigurationError("HL_MEM_RELATION_EXPANSION_MAX_DEPTH must be at least 1")
        if self.relation_discovery_mode not in {"off", "audit", "auto"}:
            raise ConfigurationError("HL_MEM_RELATION_DISCOVERY_MODE must be 'off', 'audit', or 'auto'")
        if self.relation_discovery_pool_limit < 1 or self.relation_discovery_max_proposals < 1:
            raise ConfigurationError("relation discovery limits must be positive")
        if not 0.0 <= self.relation_auto_apply_confidence <= 1.0:
            raise ConfigurationError("HL_MEM_RELATION_AUTO_APPLY_CONFIDENCE must be between 0 and 1")
        if not 0.0 <= self.relation_conflict_confidence <= 1.0:
            raise ConfigurationError("HL_MEM_RELATION_CONFLICT_CONFIDENCE must be between 0 and 1")
        if self.packed_context_token_budget < 1 or self.recall_candidate_floor < 1:
            raise ConfigurationError("recall budgets must be positive")
        if not 0.0 <= self.recall_dedup_threshold <= 1.0:
            raise ConfigurationError("HL_MEM_RECALL_DEDUP_THRESHOLD must be between 0 and 1 (0 disables fold)")
        if self.recall_dedup_candidate_limit < 1:
            raise ConfigurationError("HL_MEM_RECALL_DEDUP_CANDIDATE_LIMIT must be positive")
        if self.relevance_gate_mode not in {"off", "observe", "enforce"}:
            raise ConfigurationError("HL_MEM_RELEVANCE_GATE_MODE must be 'off', 'observe', or 'enforce'")
        relevance_thresholds = {
            "HL_MEM_RELEVANCE_RERANKER_FLOOR": self.relevance_reranker_floor,
            "HL_MEM_RELEVANCE_DENSE_FLOOR": self.relevance_dense_floor,
            "HL_MEM_RELEVANCE_RELATIVE_DROP": self.relevance_relative_drop,
        }
        for variable_name, value in relevance_thresholds.items():
            if not 0.0 <= value <= 1.0:
                raise ConfigurationError(f"{variable_name} must be between 0 and 1")
        if not isinstance(self.relevance_keep_top1, bool):
            raise ConfigurationError("HL_MEM_RELEVANCE_KEEP_TOP1 must be a boolean")
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
            raise ConfigurationError("HL_MEM_RELEVANCE_INTENTS must contain comma-separated recall intents")
        if not 0.0 <= self.preference_recency_boost <= 1.0:
            raise ConfigurationError("HL_MEM_PREFERENCE_RECENCY_BOOST must be between 0 and 1")
        if not 0.0 <= self.tag_boost_weight <= 1.0:
            raise ConfigurationError("HL_MEM_TAG_BOOST_WEIGHT must be between 0 and 1")
        if not 0.0 <= self.tag_channel_weight <= 1.0:
            raise ConfigurationError("HL_MEM_TAG_CHANNEL_WEIGHT must be between 0 and 1")
        if self.tag_candidate_limit < 1:
            raise ConfigurationError("HL_MEM_TAG_CANDIDATE_LIMIT must be positive")
        if self.query_expansion_mode not in {"off", "auto", "always"}:
            raise ConfigurationError("HL_MEM_QUERY_EXPANSION_MODE must be 'off', 'auto', or 'always'")
        if self.query_context_mode not in {"off", "coreference"}:
            raise ConfigurationError("HL_MEM_QUERY_CONTEXT_MODE must be 'off' or 'coreference'")
        if not 0 <= self.query_expansion_max <= 2:
            raise ConfigurationError("HL_MEM_QUERY_EXPANSION_MAX must be between 0 and 2")
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
            raise ConfigurationError("HL_MEM_PROCEDURE_RECALL_MODE must be 'off', 'keyword', or 'auto'")
        if not 0.0 <= self.procedure_llm_threshold <= 1.0:
            raise ConfigurationError("HL_MEM_PROCEDURE_LLM_THRESHOLD must be between 0 and 1")
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
            raise ConfigurationError("HL_MEM_VECTOR_BATCH_SIZE must be positive")
        if (
            self.hermes_circuit_failure_threshold < 1
            or self.hermes_circuit_open_seconds <= 0
            or self.hermes_prefetch_cache_ttl_seconds <= 0
        ):
            raise ConfigurationError("Hermes circuit breaker and prefetch cache values must be positive")
        if self.policy_induction_lookback_days < 1 or self.policy_induction_min_episodes < 1:
            raise ConfigurationError("policy induction values must be positive")
        if self.llm_max_attempts < 1:
            raise ConfigurationError("LLM_MAX_ATTEMPTS must be at least 1")
        if self.llm_schema_retries < 0:
            raise ConfigurationError("HL_MEM_LLM_SCHEMA_RETRIES must be non-negative")
        if self.image_describer_mode not in {"off", "on"}:
            raise ConfigurationError("HL_MEM_IMAGE_DESCRIBER_MODE must be 'off' or 'on'")
        if self.image_describer_provider != "dashscope":
            raise ConfigurationError("HL_MEM_IMAGE_DESCRIBER_PROVIDER must be 'dashscope'")
        if self.image_max_bytes < 1 or self.image_max_parts < 1 or self.image_describer_timeout_seconds <= 0:
            raise ConfigurationError("image limits and timeout must be positive")
        if self.image_describer_mode == "on":
            if not self.image_describer_base_url.lower().startswith("https://"):
                raise ConfigurationError("HL_MEM_IMAGE_DESCRIBER_BASE_URL must use HTTPS")
            if not self.image_describer_model.strip():
                raise ConfigurationError("HL_MEM_IMAGE_DESCRIBER_MODEL must not be empty")
            if self.image_allow_file_uris and not self.image_file_allow_roots:
                raise ConfigurationError("HL_MEM_IMAGE_FILE_ALLOW_ROOTS is required when file image URIs are enabled")
        if not 0.0 <= self.dedup_threshold <= 1.0:
            raise ConfigurationError("HL_MEM_DEDUP_THRESHOLD must be between 0 and 1")
        if not self.dedup_threshold <= self.dedup_auto_merge_min_confidence <= 1.0:
            raise ConfigurationError("HL_MEM_DEDUP_AUTO_MERGE_MIN_CONFIDENCE must be between dedup threshold and 1")
        if self.dedup_scan_limit < 1:
            raise ConfigurationError("HL_MEM_DEDUP_SCAN_LIMIT must be positive")
        parse_daily_cron(self.dedup_cron, "HL_MEM_DEDUP_CRON")
        if self.extraction_chunk_target_chars < 1:
            raise ConfigurationError("HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS must be positive")
        if self.extraction_chunk_overlap_turns < 0:
            raise ConfigurationError("HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS must be non-negative")
        if self.extraction_max_split_depth < 0:
            raise ConfigurationError("HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH must be non-negative")
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
            raise ConfigurationError("HL_MEM_EMBEDDER must be 'fake' or 'real'")
        if self.index_text_mode not in {"legacy", "value_only", "natural", "answerable"}:
            raise ConfigurationError(
                "HL_MEM_INDEX_TEXT_MODE must be 'legacy', 'value_only', 'natural', or 'answerable'"
            )
        if self.index_backfill_batch_size < 1 or self.index_backfill_max_attempts < 1:
            raise ConfigurationError("index backfill batch size and max attempts must be positive")
        if not self.index_text_version.strip():
            raise ConfigurationError("HL_MEM_INDEX_TEXT_VERSION must not be empty")
        if self.reranker_mode not in {"off", "fake", "on", "real"}:
            raise ConfigurationError("HL_MEM_RERANKER must be 'off', 'fake', 'on', or 'real'")
        if self.reranker_provider != "dashscope":
            raise ConfigurationError("HL_MEM_RERANKER_PROVIDER must be 'dashscope'")
        if self.extractor_mode not in {"fake", "real", "llm"}:
            raise ConfigurationError("HL_MEM_EXTRACTOR must be 'fake', 'real', or 'llm'")

    def _validate(self) -> None:
        """兼容旧调用方，委托公开配置校验入口。"""
        self.validate()

    def snapshot(self) -> dict[str, Any]:
        """返回可用于健康检查和审计的非敏感配置。"""
        return {
            "embedder_mode": self.embedder_mode,
            "embedding_dim": self.embedding_dim,
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
            "recall_vector_scan_limit": self.recall_vector_scan_limit,
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
            "recall_side_effect_max_attempts": self.recall_side_effect_max_attempts,
            "recall_side_effect_backoff_seconds": self.recall_side_effect_backoff_seconds,
            "vector_backend": self.vector_backend,
            "vector_batch_size": self.vector_batch_size,
            "extract_pre_filter": self.extract_pre_filter,
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
