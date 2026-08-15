"""v0.28 compact 提取 A/B 的冻结契约适配器。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from typing import Any, Literal

from hl_mem.components import make_llm_client
from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.llm_extractor import (
    ENGLISH_SYSTEM_PROMPT,
    LEGACY_ENGLISH_SYSTEM_PROMPT,
    LEGACY_SYSTEM_PROMPT,
    PROMPT_HASH,
    SYSTEM_PROMPT,
    LLMExtractor,
)
from hl_mem.ingest.schemas import extraction_response_json_schema
from hl_mem.ingest.verifier import EntailmentVerifier
from hl_mem.llm.types import StructuredOutputMode
from hl_mem.settings import Settings

LEGACY_CONTRACT_ID = "compact-7field-v1"
CURRENT_CONTRACT_ID = "compact-source-bounded-rao-v1"


def legacy_extraction_response_json_schema() -> dict[str, Any]:
    """从当前 schema 精确投影冻结的七字段 compact 契约。"""
    schema = deepcopy(extraction_response_json_schema())
    claim = schema["$defs"]["CompactExtractedClaimSchema"]
    for field in ("action", "object"):
        claim["properties"].pop(field)
        claim["required"].remove(field)
    return schema


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extraction_contract_snapshot(arm: Literal["old", "new"]) -> dict[str, Any]:
    """返回不含密钥的冻结 prompt/schema 身份。"""
    if arm == "old":
        contract_id = LEGACY_CONTRACT_ID
        chinese_prompt = LEGACY_SYSTEM_PROMPT
        english_prompt = LEGACY_ENGLISH_SYSTEM_PROMPT
        schema = legacy_extraction_response_json_schema()
        product_prompt_hash = None
    elif arm == "new":
        contract_id = CURRENT_CONTRACT_ID
        chinese_prompt = SYSTEM_PROMPT
        english_prompt = ENGLISH_SYSTEM_PROMPT
        schema = extraction_response_json_schema()
        product_prompt_hash = PROMPT_HASH
    else:
        raise ValueError(f"unsupported extraction arm: {arm}")
    payload = {
        "contract_id": contract_id,
        "chinese_prompt_sha256": _canonical_hash(chinese_prompt),
        "english_prompt_sha256": _canonical_hash(english_prompt),
        "response_schema_sha256": _canonical_hash(schema),
        "product_prompt_hash": product_prompt_hash,
        "postprocess_baseline": "v028-current",
    }
    return {**payload, "contract_sha256": _canonical_hash(payload)}


_LEGACY_HASH = extraction_contract_snapshot("old")["contract_sha256"][:12]


class LegacyCompactLLMExtractor(LLMExtractor):
    """仅供冻结 A/B 使用的七字段提取器；不暴露为产品配置。"""

    prompt_hash = _LEGACY_HASH
    extractor_version = f"{LEGACY_CONTRACT_ID}+{_LEGACY_HASH}"

    def _system_prompt_for_language(self, language: Literal["zh", "en"]) -> str:
        return LEGACY_ENGLISH_SYSTEM_PROMPT if language == "en" else LEGACY_SYSTEM_PROMPT

    def _response_json_schema(self) -> dict[str, Any]:
        return legacy_extraction_response_json_schema()


def make_extraction_arm_extractor(
    settings: Settings,
    connection: sqlite3.Connection,
    arm: Literal["old", "new"],
) -> LLMExtractor:
    """按生产参数构造冻结 arm，不引入长期 Settings 开关。"""
    if arm not in {"old", "new"}:
        raise ValueError(f"unsupported extraction arm: {arm}")
    structured_mode = (
        StructuredOutputMode.JSON_OBJECT
        if settings.llm_structured_mode == "json_object"
        else StructuredOutputMode.JSON_SCHEMA
    )
    client = make_llm_client(settings, connection, operation=f"extract_ab_{arm}")
    verifier = (
        EntailmentVerifier(client, structured_mode=structured_mode) if settings.verification_mode != "off" else None
    )
    extractor_type = LegacyCompactLLMExtractor if arm == "old" else LLMExtractor
    return extractor_type(
        client,
        ChunkingPolicy(
            target_chars=settings.extraction_chunk_target_chars,
            overlap_turns=settings.extraction_chunk_overlap_turns,
            max_split_depth=settings.extraction_max_split_depth,
        ),
        schema_retries=settings.llm_schema_retries,
        structured_mode=structured_mode,
        verifier=verifier,
        verification_mode=settings.verification_mode,
    )
