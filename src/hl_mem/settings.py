"""集中化配置入口：启动时解析一次并校验配置组合。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.retention import TTLPolicy
from hl_mem.domain.entity import load_entity_aliases, set_active_aliases
from hl_mem.errors import ConfigurationError


def parse_daily_cron(value: str, variable_name: str) -> int:
    """严格解析每日 HH:MM 配置并返回自午夜起的分钟数。"""
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise ConfigurationError(f"{variable_name} must use strict HH:MM format")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


@dataclass(frozen=True)
class Settings:
    """全局非敏感配置快照。"""

    environment: str = "dev"
    database_path: str = "var/hl_mem.db"
    allow_fake_fallback: bool = False
    embedder_mode: str = "fake"
    embedding_dim: int = 2048
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_connect_timeout: float = 5.0
    embedding_read_timeout: float = 30.0
    embedding_max_attempts: int = 3
    reranker_mode: str = "off"
    reranker_provider: str = "dashscope"
    reranker_api_key: str | None = None
    reranker_base_url: str = "https://dashscope.aliyuncs.com"
    reranker_model: str = "gte-rerank-v2"
    relation_expansion_mode: str = "off"
    relation_expansion_max_depth: int = 1
    packed_context_token_budget: int = 2000
    recall_candidate_floor: int = 50
    preference_recency_boost: float = 0.12
    tag_boost_enabled: bool = True
    tag_boost_weight: float = 0.05
    tag_channel_enabled: bool = False
    tag_channel_weight: float = 0.15
    tag_candidate_limit: int = 20
    fts_tokenizer: str = "unicode61"
    vector_backend: str = "sqlite_scan"
    hermes_circuit_failure_threshold: int = 5
    hermes_circuit_open_seconds: float = 60.0
    hermes_prefetch_cache_ttl_seconds: float = 300.0
    policy_induction_lookback_days: int = 7
    policy_induction_min_episodes: int = 3
    extractor_mode: str = "fake"
    llm_api_key: str | None = None
    llm_base_url: str = "https://coding.dashscope.aliyuncs.com/v1"
    llm_model: str = "qwen3.7-plus"
    llm_provider: str = "dashscope"
    llm_structured_mode: str = "auto"
    llm_timeout: float = 90.0
    llm_max_attempts: int = 3
    llm_schema_retries: int = 2
    extraction_chunk_target_chars: int = 12000
    extraction_chunk_overlap_turns: int = 2
    extraction_max_split_depth: int = 3
    worker_poll_interval: float = 2.0
    worker_maintenance_interval: float = 600.0
    worker_job_lease_minutes: int = 5
    daily_token_limit: int = 500000
    audit_retention_days: int = 30
    retention_days: int = 30
    consolidate_cron: str = "03:30"
    consolidate_batch_size: int = 100
    consolidate_confidence: float = 0.8
    dedup_enabled: bool = True
    dedup_threshold: float = 0.92
    dedup_audit_only: bool = True
    dedup_auto_merge_min_confidence: float = 0.98
    dedup_scan_limit: int = 200
    dedup_cron: str = "03:00"
    induce_policies_cron: str = "04:00"
    reclassify_cron: str = "04:30"
    memory_temporal_ttl_days: int = 7
    temporal_ttl_days_low: int = 3
    temporal_ttl_days_normal: int = 7
    temporal_ttl_days_high: int = 14
    importance_low_threshold: float = 0.4
    importance_high_threshold: float = 0.7
    importance_write_floor: float = 0.2
    slot_short_ttl_seconds: int = 86400
    ttl_backfill_batch_size: int = 100
    ttl_backfill_grace_hours: int = 0
    max_request_body: int = 2 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量创建并校验不可变配置快照。"""
        environment = os.getenv("HL_MEM_ENV", "dev").lower()
        production = environment == "production"
        settings = cls(
            environment=environment,
            database_path=os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db"),
            allow_fake_fallback=os.getenv("HL_MEM_ALLOW_FAKE_FALLBACK", "").lower() == "true",
            embedder_mode=os.getenv("HL_MEM_EMBEDDER", "real" if production else "fake").lower(),
            embedding_dim=int(os.getenv("EMBEDDING_DIM", "2048")),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY"),
            embedding_base_url=os.getenv(
                "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            embedding_connect_timeout=float(os.getenv("EMBEDDING_CONNECT_TIMEOUT", "5")),
            embedding_read_timeout=float(os.getenv("EMBEDDING_READ_TIMEOUT", "30")),
            embedding_max_attempts=int(os.getenv("EMBEDDING_MAX_ATTEMPTS", "3")),
            reranker_mode=os.getenv("HL_MEM_RERANKER", "real" if production else "off").lower(),
            reranker_provider=os.getenv("HL_MEM_RERANKER_PROVIDER", "dashscope").lower(),
            reranker_api_key=os.getenv("RERANKER_API_KEY") or os.getenv("EMBEDDING_API_KEY"),
            reranker_base_url=os.getenv("RERANKER_BASE_URL", "https://dashscope.aliyuncs.com"),
            reranker_model=os.getenv("RERANKER_MODEL", "gte-rerank-v2"),
            relation_expansion_mode=os.getenv("HL_MEM_RELATION_EXPANSION", "off").lower(),
            relation_expansion_max_depth=int(os.getenv("HL_MEM_RELATION_EXPANSION_MAX_DEPTH", "1")),
            packed_context_token_budget=int(os.getenv("HL_MEM_PACKED_CONTEXT_TOKEN_BUDGET", "2000")),
            recall_candidate_floor=int(os.getenv("HL_MEM_RECALL_CANDIDATE_FLOOR", "50")),
            preference_recency_boost=float(os.getenv("HL_MEM_PREFERENCE_RECENCY_BOOST", "0.12")),
            tag_boost_enabled=os.getenv("HL_MEM_TAG_BOOST_ENABLED", "true").lower() == "true",
            tag_boost_weight=float(os.getenv("HL_MEM_TAG_BOOST_WEIGHT", "0.05")),
            tag_channel_enabled=os.getenv("HL_MEM_TAG_CHANNEL_ENABLED", "false").lower() == "true",
            tag_channel_weight=float(os.getenv("HL_MEM_TAG_CHANNEL_WEIGHT", "0.15")),
            tag_candidate_limit=int(os.getenv("HL_MEM_TAG_CANDIDATE_LIMIT", "20")),
            fts_tokenizer=os.getenv("HL_MEM_FTS_TOKENIZER", "unicode61"),
            vector_backend=os.getenv("HL_MEM_VECTOR_BACKEND", "sqlite_scan"),
            hermes_circuit_failure_threshold=int(os.getenv("HL_MEM_HERMES_CIRCUIT_FAILURE_THRESHOLD", "5")),
            hermes_circuit_open_seconds=float(os.getenv("HL_MEM_HERMES_CIRCUIT_OPEN_SECONDS", "60")),
            hermes_prefetch_cache_ttl_seconds=float(os.getenv("HL_MEM_HERMES_PREFETCH_CACHE_TTL_SECONDS", "300")),
            policy_induction_lookback_days=int(os.getenv("HL_MEM_POLICY_INDUCTION_LOOKBACK_DAYS", "7")),
            policy_induction_min_episodes=int(os.getenv("HL_MEM_POLICY_INDUCTION_MIN_EPISODES", "3")),
            extractor_mode=os.getenv("HL_MEM_EXTRACTOR", "fake").lower(),
            llm_api_key=os.getenv("LLM_API_KEY"),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
            llm_model=os.getenv("LLM_MODEL", "qwen3.7-plus"),
            llm_provider=os.getenv("HL_MEM_LLM_PROVIDER", "dashscope").lower(),
            llm_structured_mode=os.getenv("HL_MEM_LLM_STRUCTURED_MODE", "auto").lower(),
            llm_timeout=float(os.getenv("LLM_TIMEOUT", "90")),
            llm_max_attempts=int(os.getenv("LLM_MAX_ATTEMPTS", "3")),
            llm_schema_retries=int(os.getenv("HL_MEM_LLM_SCHEMA_RETRIES", "2")),
            extraction_chunk_target_chars=int(os.getenv("HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS", "12000")),
            extraction_chunk_overlap_turns=int(os.getenv("HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS", "2")),
            extraction_max_split_depth=int(os.getenv("HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH", "3")),
            worker_poll_interval=float(os.getenv("HL_MEM_WORKER_POLL_INTERVAL", "2.0")),
            worker_maintenance_interval=float(os.getenv("HL_MEM_WORKER_MAINTENANCE_INTERVAL", "600")),
            worker_job_lease_minutes=int(os.getenv("HL_MEM_WORKER_LEASE_MINUTES", "5")),
            daily_token_limit=int(os.getenv("HL_MEM_DAILY_TOKEN_LIMIT", "500000")),
            audit_retention_days=int(
                os.getenv("HL_MEM_AUDIT_RETENTION_DAYS", os.getenv("HL_MEM_RETENTION_DAYS", "30"))
            ),
            retention_days=int(os.getenv("HL_MEM_RETENTION_DAYS", "30")),
            consolidate_cron=os.getenv("HL_MEM_CONSOLIDATE_CRON", "03:30"),
            consolidate_batch_size=int(os.getenv("HL_MEM_CONSOLIDATE_BATCH_SIZE", "100")),
            consolidate_confidence=float(os.getenv("HL_MEM_CONSOLIDATE_CONFIDENCE", "0.8")),
            dedup_enabled=os.getenv("HL_MEM_DEDUP_ENABLED", "true").lower() == "true",
            dedup_threshold=float(os.getenv("HL_MEM_DEDUP_THRESHOLD", "0.92")),
            dedup_audit_only=os.getenv("HL_MEM_DEDUP_AUDIT_ONLY", "true").lower() == "true",
            dedup_auto_merge_min_confidence=float(
                os.getenv("HL_MEM_DEDUP_AUTO_MERGE_MIN_CONFIDENCE", "0.98")
            ),
            dedup_scan_limit=int(os.getenv("HL_MEM_DEDUP_SCAN_LIMIT", "200")),
            dedup_cron=os.getenv("HL_MEM_DEDUP_CRON", "03:00"),
            induce_policies_cron=os.getenv("HL_MEM_INDUCE_POLICIES_CRON", "04:00"),
            reclassify_cron=os.getenv("HL_MEM_RECLASSIFY_CRON", "04:30"),
            memory_temporal_ttl_days=int(os.getenv("HL_MEM_TEMPORAL_TTL_DAYS", "7")),
            temporal_ttl_days_low=int(os.getenv("HL_MEM_TEMPORAL_TTL_DAYS_LOW", "3")),
            temporal_ttl_days_normal=int(os.getenv("HL_MEM_TEMPORAL_TTL_DAYS_NORMAL", "7")),
            temporal_ttl_days_high=int(os.getenv("HL_MEM_TEMPORAL_TTL_DAYS_HIGH", "14")),
            importance_low_threshold=float(os.getenv("HL_MEM_IMPORTANCE_LOW_THRESHOLD", "0.4")),
            importance_high_threshold=float(os.getenv("HL_MEM_IMPORTANCE_HIGH_THRESHOLD", "0.7")),
            importance_write_floor=float(os.getenv("HL_MEM_IMPORTANCE_WRITE_FLOOR", "0.2")),
            slot_short_ttl_seconds=int(os.getenv("HL_MEM_SLOT_SHORT_TTL_SECONDS", "86400")),
            ttl_backfill_batch_size=int(os.getenv("HL_MEM_TTL_BACKFILL_BATCH_SIZE", "100")),
            ttl_backfill_grace_hours=int(os.getenv("HL_MEM_TTL_BACKFILL_GRACE_HOURS", "0")),
            max_request_body=int(os.getenv("HL_MEM_MAX_REQUEST_BODY", str(2 * 1024 * 1024))),
        )
        settings._validate()
        set_active_aliases(load_entity_aliases())
        return settings

    def _validate(self) -> None:
        """校验生产环境所需的安全配置组合。"""
        if self.llm_provider not in {"dashscope", "zhipu", "openai_compatible"}:
            raise ConfigurationError("HL_MEM_LLM_PROVIDER must be 'dashscope', 'zhipu', or 'openai_compatible'")
        if self.llm_structured_mode not in {"auto", "json_object", "json_schema"}:
            raise ConfigurationError("HL_MEM_LLM_STRUCTURED_MODE must be 'auto', 'json_object', or 'json_schema'")
        if self.relation_expansion_mode not in {"off", "on"}:
            raise ConfigurationError("HL_MEM_RELATION_EXPANSION must be 'off' or 'on'")
        if self.relation_expansion_max_depth < 1:
            raise ConfigurationError("HL_MEM_RELATION_EXPANSION_MAX_DEPTH must be at least 1")
        if self.packed_context_token_budget < 1 or self.recall_candidate_floor < 1:
            raise ConfigurationError("recall budgets must be positive")
        if not 0.0 <= self.preference_recency_boost <= 1.0:
            raise ConfigurationError("HL_MEM_PREFERENCE_RECENCY_BOOST must be between 0 and 1")
        if not 0.0 <= self.tag_boost_weight <= 1.0:
            raise ConfigurationError("HL_MEM_TAG_BOOST_WEIGHT must be between 0 and 1")
        if not 0.0 <= self.tag_channel_weight <= 1.0:
            raise ConfigurationError("HL_MEM_TAG_CHANNEL_WEIGHT must be between 0 and 1")
        if self.tag_candidate_limit < 1:
            raise ConfigurationError("HL_MEM_TAG_CANDIDATE_LIMIT must be positive")
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
        if not 0.0 <= self.dedup_threshold <= 1.0:
            raise ConfigurationError("HL_MEM_DEDUP_THRESHOLD must be between 0 and 1")
        if not self.dedup_threshold <= self.dedup_auto_merge_min_confidence <= 1.0:
            raise ConfigurationError(
                "HL_MEM_DEDUP_AUTO_MERGE_MIN_CONFIDENCE must be between dedup threshold and 1"
            )
        if self.dedup_scan_limit < 1:
            raise ConfigurationError("HL_MEM_DEDUP_SCAN_LIMIT must be positive")
        parse_daily_cron(self.dedup_cron, "HL_MEM_DEDUP_CRON")
        if self.extraction_chunk_target_chars < 1:
            raise ConfigurationError("HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS must be positive")
        if self.extraction_chunk_overlap_turns < 0:
            raise ConfigurationError("HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS must be non-negative")
        if self.extraction_max_split_depth < 0:
            raise ConfigurationError("HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH must be non-negative")
        if min(
            self.temporal_ttl_days_low,
            self.temporal_ttl_days_normal,
            self.temporal_ttl_days_high,
            self.slot_short_ttl_seconds,
        ) < 1:
            raise ConfigurationError("TTL durations must be positive")
        if self.ttl_backfill_batch_size < 1 or self.ttl_backfill_grace_hours < 0:
            raise ConfigurationError("TTL backfill batch size must be positive and grace hours non-negative")
        if not (
            0.0
            <= self.importance_write_floor
            <= self.importance_low_threshold
            <= self.importance_high_threshold
            <= 1.0
        ):
            raise ConfigurationError("importance thresholds must be ordered between 0 and 1")
        if self.embedder_mode not in {"fake", "real"}:
            raise ConfigurationError("HL_MEM_EMBEDDER must be 'fake' or 'real'")
        if self.reranker_mode not in {"off", "fake", "on", "real"}:
            raise ConfigurationError("HL_MEM_RERANKER must be 'off', 'fake', 'on', or 'real'")
        if self.reranker_provider != "dashscope":
            raise ConfigurationError("HL_MEM_RERANKER_PROVIDER must be 'dashscope'")
        if self.extractor_mode not in {"fake", "real", "llm"}:
            raise ConfigurationError("HL_MEM_EXTRACTOR must be 'fake', 'real', or 'llm'")
        if self.environment != "production":
            return
        if self.embedder_mode != "real":
            raise ConfigurationError("HL_MEM_EMBEDDER must be 'real' in production")
        if self.reranker_mode not in {"on", "real"}:
            raise ConfigurationError("HL_MEM_RERANKER must be enabled in production")
        if self.extractor_mode == "fake":
            raise ConfigurationError("HL_MEM_EXTRACTOR must not be 'fake' in production")
        if not self.llm_api_key:
            raise ConfigurationError("LLM_API_KEY is required in production")
        if not self.embedding_api_key:
            raise ConfigurationError("EMBEDDING_API_KEY is required in production")

    def snapshot(self) -> dict[str, Any]:
        """返回可用于健康检查和审计的非敏感配置。"""
        return {
            "environment": self.environment,
            "embedder_mode": self.embedder_mode,
            "embedding_dim": self.embedding_dim,
            "reranker_mode": self.reranker_mode,
            "reranker_provider": self.reranker_provider,
            "relation_expansion_mode": self.relation_expansion_mode,
            "relation_expansion_max_depth": self.relation_expansion_max_depth,
            "tag_boost_enabled": self.tag_boost_enabled,
            "tag_boost_weight": self.tag_boost_weight,
            "tag_channel_enabled": self.tag_channel_enabled,
            "tag_channel_weight": self.tag_channel_weight,
            "tag_candidate_limit": self.tag_candidate_limit,
            "vector_backend": self.vector_backend,
            "llm_model": self.llm_model,
            "llm_provider": self.llm_provider,
            "llm_structured_mode": self.llm_structured_mode,
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
