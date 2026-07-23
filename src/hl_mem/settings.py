"""集中化配置入口：启动时解析一次并校验配置组合。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.entity import load_entity_aliases, set_active_aliases
from hl_mem.errors import ConfigurationError


@dataclass(frozen=True)
class Settings:
    """全局非敏感配置快照。"""

    environment: str = "dev"
    database_path: str = "var/hl_mem.db"
    embedder_mode: str = "fake"
    embedding_dim: int = 2048
    embedding_model: str = "text-embedding-v4"
    reranker_mode: str = "off"
    llm_model: str = "qwen3.7-plus"
    llm_provider: str = "dashscope"
    llm_structured_mode: str = "auto"
    llm_max_attempts: int = 3
    llm_schema_retries: int = 2
    extraction_chunk_target_chars: int = 12000
    extraction_chunk_overlap_turns: int = 2
    extraction_max_split_depth: int = 3
    worker_poll_interval: float = 2.0
    worker_maintenance_interval: float = 600.0
    max_request_body: int = 2 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量创建并校验不可变配置快照。"""
        environment = os.getenv("HL_MEM_ENV", "dev").lower()
        production = environment == "production"
        settings = cls(
            environment=environment,
            database_path=os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db"),
            embedder_mode=os.getenv("HL_MEM_EMBEDDER", "real" if production else "fake").lower(),
            embedding_dim=int(os.getenv("EMBEDDING_DIM", "2048")),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            reranker_mode=os.getenv("HL_MEM_RERANKER", "real" if production else "off").lower(),
            llm_model=os.getenv("LLM_MODEL", "qwen3.7-plus"),
            llm_provider=os.getenv("HL_MEM_LLM_PROVIDER", "dashscope").lower(),
            llm_structured_mode=os.getenv("HL_MEM_LLM_STRUCTURED_MODE", "auto").lower(),
            llm_max_attempts=int(os.getenv("LLM_MAX_ATTEMPTS", "3")),
            llm_schema_retries=int(os.getenv("HL_MEM_LLM_SCHEMA_RETRIES", "2")),
            extraction_chunk_target_chars=int(os.getenv("HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS", "12000")),
            extraction_chunk_overlap_turns=int(os.getenv("HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS", "2")),
            extraction_max_split_depth=int(os.getenv("HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH", "3")),
            worker_poll_interval=float(os.getenv("HL_MEM_WORKER_POLL_INTERVAL", "2.0")),
            worker_maintenance_interval=float(os.getenv("HL_MEM_WORKER_MAINTENANCE_INTERVAL", "600")),
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
        if self.llm_max_attempts < 1:
            raise ConfigurationError("LLM_MAX_ATTEMPTS must be at least 1")
        if self.llm_schema_retries < 0:
            raise ConfigurationError("HL_MEM_LLM_SCHEMA_RETRIES must be non-negative")
        if self.extraction_chunk_target_chars < 1:
            raise ConfigurationError("HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS must be positive")
        if self.extraction_chunk_overlap_turns < 0:
            raise ConfigurationError("HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS must be non-negative")
        if self.extraction_max_split_depth < 0:
            raise ConfigurationError("HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH must be non-negative")
        if self.environment != "production":
            return
        if self.embedder_mode != "real":
            raise ConfigurationError("HL_MEM_EMBEDDER must be 'real' in production")
        if self.reranker_mode not in {"on", "real"}:
            raise ConfigurationError("HL_MEM_RERANKER must be enabled in production")
        if not os.getenv("LLM_API_KEY"):
            raise ConfigurationError("LLM_API_KEY is required in production")
        if not os.getenv("EMBEDDING_API_KEY"):
            raise ConfigurationError("EMBEDDING_API_KEY is required in production")

    def snapshot(self) -> dict[str, Any]:
        """返回可用于健康检查和审计的非敏感配置。"""
        return {
            "environment": self.environment,
            "embedder_mode": self.embedder_mode,
            "embedding_dim": self.embedding_dim,
            "reranker_mode": self.reranker_mode,
            "llm_model": self.llm_model,
            "llm_provider": self.llm_provider,
            "llm_structured_mode": self.llm_structured_mode,
            "extraction_chunk_target_chars": self.extraction_chunk_target_chars,
            "extraction_chunk_overlap_turns": self.extraction_chunk_overlap_turns,
            "extraction_max_split_depth": self.extraction_max_split_depth,
        }
