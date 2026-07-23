"""统一组件工厂。集中管理 embedder、reranker、extractor 的创建逻辑和环境变量配置。"""

from __future__ import annotations

import os
from typing import Any

import httpx

from hl_mem.config import (
    EXTRACTION_CHUNK_OVERLAP_TURNS,
    EXTRACTION_CHUNK_TARGET_CHARS,
    EXTRACTION_MAX_SPLIT_DEPTH,
)
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embeddings import Embedder, FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import DashScopeProvider, OpenAICompatibleProvider, ZhipuProvider
from hl_mem.llm.types import StructuredOutputMode
from hl_mem.recall.reranker import FakeReranker, Reranker

_EXTRACTOR_REGISTRY: dict[str, str] = {
    "message": "llm",
    "explicit_memory": "explicit",
    "tool_result": "llm",
}


def _allow_fake_fallback() -> bool:
    """返回是否允许显式真实组件在缺少密钥时降级。"""
    return os.getenv("HL_MEM_ALLOW_FAKE_FALLBACK", "").lower() == "true"


def make_embedder(config: dict[str, Any] | None = None) -> Any:
    """从环境变量与可选配置创建向量化组件。"""
    settings = config or {}
    dim = int(settings.get("embedding_dim", os.getenv("EMBEDDING_DIM", "2048")))
    production = os.getenv("HL_MEM_ENV", "dev").lower() == "production"
    explicit = os.getenv("HL_MEM_EMBEDDER") is not None
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
        if explicit and not _allow_fake_fallback():
            raise ConfigurationError("HL_MEM_EMBEDDER=real but EMBEDDING_API_KEY is missing")
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
    explicit = os.getenv("HL_MEM_RERANKER") is not None
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
        if explicit and not _allow_fake_fallback():
            raise ConfigurationError(f"HL_MEM_RERANKER={mode} but RERANKER_API_KEY or EMBEDDING_API_KEY is missing")
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
    explicit = os.getenv("HL_MEM_EXTRACTOR") is not None
    extractor_name = str(settings.get("extractor_name", os.getenv("HL_MEM_EXTRACTOR", "fake"))).lower()
    if production and extractor_name == "fake":
        raise ConfigurationError("HL_MEM_EXTRACTOR must not be 'fake' in production")
    if extractor_name == "fake":
        return FakeExtractor()
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        if production or settings.get("require_real"):
            raise ConfigurationError("LLM_API_KEY is required in production")
        if explicit and not _allow_fake_fallback():
            raise ConfigurationError(f"HL_MEM_EXTRACTOR={extractor_name} but LLM_API_KEY is missing")
        return FakeExtractor()
    provider_name = str(settings.get("llm_provider", os.getenv("HL_MEM_LLM_PROVIDER", "dashscope"))).lower()
    provider_types = {
        "dashscope": DashScopeProvider,
        "zhipu": ZhipuProvider,
        "openai_compatible": OpenAICompatibleProvider,
    }
    provider_type = provider_types.get(provider_name)
    if provider_type is None:
        raise ConfigurationError("HL_MEM_LLM_PROVIDER must be 'dashscope', 'zhipu', or 'openai_compatible'")
    base_url = os.getenv("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1")
    model = os.getenv("LLM_MODEL", "qwen3.7-plus")
    timeout_seconds = float(os.getenv("LLM_TIMEOUT", "90"))
    structured_mode_name = os.getenv("HL_MEM_LLM_STRUCTURED_MODE", "auto").lower()
    if structured_mode_name not in {"auto", "json_object", "json_schema"}:
        raise ConfigurationError("HL_MEM_LLM_STRUCTURED_MODE must be 'auto', 'json_object', or 'json_schema'")
    structured_mode = (
        StructuredOutputMode.JSON_OBJECT if structured_mode_name == "json_object" else StructuredOutputMode.JSON_SCHEMA
    )
    llm_client = LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider=provider_type(),
        timeout=httpx.Timeout(timeout_seconds),
        max_attempts=int(os.getenv("LLM_MAX_ATTEMPTS", "3")),
    )
    return LLMExtractor(
        api_key,
        base_url,
        model,
        timeout=timeout_seconds,
        llm_client=llm_client,
        schema_retries=int(os.getenv("HL_MEM_LLM_SCHEMA_RETRIES", "2")),
        structured_mode=structured_mode,
        chunking_policy=ChunkingPolicy(
            target_chars=int(
                settings.get(
                    "extraction_chunk_target_chars",
                    os.getenv(
                        "HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS",
                        str(EXTRACTION_CHUNK_TARGET_CHARS),
                    ),
                )
            ),
            overlap_turns=int(
                settings.get(
                    "extraction_chunk_overlap_turns",
                    os.getenv(
                        "HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS",
                        str(EXTRACTION_CHUNK_OVERLAP_TURNS),
                    ),
                )
            ),
            max_split_depth=int(
                settings.get(
                    "extraction_max_split_depth",
                    os.getenv(
                        "HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH",
                        str(EXTRACTION_MAX_SPLIT_DEPTH),
                    ),
                )
            ),
        ),
    )


def make_extractor_for_type(event_type: str, config: dict[str, Any] | None = None) -> Any:
    """根据事件类型选择提取器；显式记忆返回 worker 可识别的特殊标记。"""
    extractor_name = _EXTRACTOR_REGISTRY.get(event_type, "llm")
    if extractor_name == "explicit":
        return "explicit"
    return make_extractor(config)
