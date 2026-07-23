"""集中化配置入口：启动时解析一次并校验配置组合。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

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
    worker_poll_interval: float = 2.0
    worker_maintenance_interval: float = 600.0

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
            worker_poll_interval=float(os.getenv("HL_MEM_WORKER_POLL_INTERVAL", "2.0")),
            worker_maintenance_interval=float(os.getenv("HL_MEM_WORKER_MAINTENANCE_INTERVAL", "600")),
        )
        settings._validate()
        return settings

    def _validate(self) -> None:
        """校验生产环境所需的安全配置组合。"""
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
        }
