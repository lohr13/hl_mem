"""统一组件工厂。集中管理 embedder、reranker、extractor 的创建逻辑和环境变量配置。"""

from __future__ import annotations

import os
from typing import Any

from hl_mem.errors import ConfigurationError
from hl_mem.ingest.embeddings import Embedder, FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.recall.reranker import FakeReranker, Reranker

_EXTRACTOR_REGISTRY: dict[str, str] = {
    "message": "llm",
    "explicit_memory": "explicit",
    "tool_result": "llm",
}


def make_embedder(config: dict[str, Any] | None = None) -> Any:
    """从环境变量与可选配置创建向量化组件。"""
    settings = config or {}
    dim = int(settings.get("embedding_dim", os.getenv("EMBEDDING_DIM", "2048")))
    production = os.getenv("HL_MEM_ENV", "dev").lower() == "production"
    mode = str(settings.get("embedder_name", os.getenv("HL_MEM_EMBEDDER", "real" if production else "fake"))).lower()
    if production and mode != "real":
        raise ConfigurationError("HL_MEM_EMBEDDER must be 'real' in production")
    if mode == "fake":
        return FakeEmbedder(dim)
    if mode != "real":
        raise ValueError("HL_MEM_EMBEDDER must be 'fake' or 'real'")
    api_key = os.getenv("EMBEDDING_API_KEY")
    if not api_key:
        if production:
            raise ConfigurationError("EMBEDDING_API_KEY is required in production")
        return FakeEmbedder(dim)
    return Embedder(
        api_key,
        os.getenv("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        dim,
        float(os.getenv("EMBEDDING_CONNECT_TIMEOUT", "5")),
        float(os.getenv("EMBEDDING_READ_TIMEOUT", "30")),
        int(os.getenv("EMBEDDING_MAX_ATTEMPTS", "3")),
    )


def make_reranker(config: dict[str, Any] | None = None) -> Any | None:
    """从环境变量与可选配置创建重排组件。"""
    settings = config or {}
    production = os.getenv("HL_MEM_ENV", "dev").lower() == "production"
    mode = str(settings.get("reranker_name", os.getenv("HL_MEM_RERANKER", "real" if production else "off"))).lower()
    if production and mode not in {"on", "real"}:
        raise ConfigurationError("HL_MEM_RERANKER must be enabled in production")
    if mode == "off":
        return None
    if mode == "fake":
        return FakeReranker()
    if mode not in {"on", "real"}:
        raise ValueError("HL_MEM_RERANKER must be 'off', 'fake', 'on', or 'real'")
    api_key = os.getenv("RERANKER_API_KEY") or os.getenv("EMBEDDING_API_KEY")
    if not api_key:
        if production:
            raise ConfigurationError("RERANKER_API_KEY or EMBEDDING_API_KEY is required in production")
        return None
    try:
        return Reranker(
            api_key,
            os.getenv("RERANKER_BASE_URL", "https://dashscope.aliyuncs.com"),
            os.getenv("RERANKER_MODEL", "gte-rerank-v2"),
        )
    except Exception:
        if production:
            raise
        return None


def make_extractor(config: dict[str, Any] | None = None) -> Any:
    """从环境变量与可选配置创建 LLM 提取组件。"""
    settings = config or {}
    production = os.getenv("HL_MEM_ENV", "dev").lower() == "production"
    extractor_name = str(settings.get("extractor_name", os.getenv("HL_MEM_EXTRACTOR", "fake"))).lower()
    if production and extractor_name == "fake":
        raise ConfigurationError("HL_MEM_EXTRACTOR must not be 'fake' in production")
    if extractor_name == "fake":
        return FakeExtractor()
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        if production or settings.get("require_real"):
            raise ConfigurationError("LLM_API_KEY is required in production")
        return FakeExtractor()
    return LLMExtractor(
        api_key,
        os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
        os.getenv("LLM_MODEL", "qwen3.7-plus"),
    )


def make_extractor_for_type(event_type: str, config: dict[str, Any] | None = None) -> Any:
    """根据事件类型选择提取器；显式记忆返回 worker 可识别的特殊标记。"""
    extractor_name = _EXTRACTOR_REGISTRY.get(event_type, "llm")
    if extractor_name == "explicit":
        return "explicit"
    return make_extractor(config)
