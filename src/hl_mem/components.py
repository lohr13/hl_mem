"""统一组件工厂，所有运行时配置均由 Settings 显式注入。"""

from __future__ import annotations

from typing import Any

import httpx

from hl_mem.errors import ConfigurationError
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.embedder import Embedder, FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.ingest.llm_extractor import LLMExtractor
from hl_mem.llm.client import LLMClient
from hl_mem.llm.providers import DashScopeProvider, OpenAICompatibleProvider, ZhipuProvider
from hl_mem.llm.types import StructuredOutputMode
from hl_mem.recall.reranker import FakeReranker, Reranker
from hl_mem.settings import Settings

_EXTRACTOR_REGISTRY: dict[str, str] = {
    "message": "llm",
    "explicit_memory": "explicit",
    "tool_result": "llm",
}


def make_llm_client(settings: Settings) -> LLMClient:
    """依据统一配置创建 provider 无关的 LLM 客户端。"""
    if not settings.llm_api_key:
        raise ConfigurationError("LLM_API_KEY is required")
    provider_types = {
        "dashscope": DashScopeProvider,
        "zhipu": ZhipuProvider,
        "openai_compatible": OpenAICompatibleProvider,
    }
    provider_type = provider_types.get(settings.llm_provider)
    if provider_type is None:
        raise ConfigurationError("HL_MEM_LLM_PROVIDER must be 'dashscope', 'zhipu', or 'openai_compatible'")
    return LLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        provider=provider_type(),
        timeout=httpx.Timeout(settings.llm_timeout),
        max_attempts=settings.llm_max_attempts,
    )


def make_embedder(settings: Settings) -> Any:
    """依据统一配置创建向量化组件。"""
    if settings.embedder_mode == "fake":
        return FakeEmbedder(settings.embedding_dim)
    if not settings.embedding_api_key:
        if settings.environment == "production" or not settings.allow_fake_fallback:
            raise ConfigurationError("HL_MEM_EMBEDDER=real but EMBEDDING_API_KEY is missing")
        return FakeEmbedder(settings.embedding_dim)
    return Embedder(
        settings.embedding_api_key,
        settings.embedding_base_url,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_connect_timeout,
        settings.embedding_read_timeout,
        settings.embedding_max_attempts,
    )


def make_reranker(settings: Settings) -> Any | None:
    """依据统一配置创建重排组件。"""
    if settings.reranker_mode == "off":
        return None
    if settings.reranker_mode == "fake":
        return FakeReranker()
    if not settings.reranker_api_key:
        if settings.environment == "production" or not settings.allow_fake_fallback:
            raise ConfigurationError(
                f"HL_MEM_RERANKER={settings.reranker_mode} but "
                "RERANKER_API_KEY or EMBEDDING_API_KEY is missing"
            )
        return None
    try:
        return Reranker(
            settings.reranker_api_key,
            settings.reranker_base_url,
            settings.reranker_model,
        )
    except Exception:
        if settings.environment == "production":
            raise
        return None


def make_extractor(settings: Settings, *, require_real: bool = False) -> Any:
    """依据统一配置创建 LLM 提取组件。"""
    if settings.extractor_mode == "fake" and not require_real:
        if settings.environment == "production":
            raise ConfigurationError("HL_MEM_EXTRACTOR=fake is not allowed in production")
        return FakeExtractor()
    if not settings.llm_api_key:
        if settings.environment == "production" or require_real or not settings.allow_fake_fallback:
            raise ConfigurationError("LLM_API_KEY is required")
        return FakeExtractor()
    structured_mode = (
        StructuredOutputMode.JSON_OBJECT
        if settings.llm_structured_mode == "json_object"
        else StructuredOutputMode.JSON_SCHEMA
    )
    return LLMExtractor(
        make_llm_client(settings),
        ChunkingPolicy(
            target_chars=settings.extraction_chunk_target_chars,
            overlap_turns=settings.extraction_chunk_overlap_turns,
            max_split_depth=settings.extraction_max_split_depth,
        ),
        schema_retries=settings.llm_schema_retries,
        structured_mode=structured_mode,
    )


def make_extractor_for_type(event_type: str, settings: Settings) -> Any:
    """根据事件类型选择提取器；显式记忆返回 worker 可识别的特殊标记。"""
    extractor_name = _EXTRACTOR_REGISTRY.get(event_type, "llm")
    if extractor_name == "explicit":
        return "explicit"
    return make_extractor(settings)
