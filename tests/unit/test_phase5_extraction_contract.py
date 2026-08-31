from __future__ import annotations

import hashlib
import json

import pytest

from hl_mem.ingest.chunking import ChunkingPolicy
from hl_mem.ingest.extraction.prompts import ENGLISH_SYSTEM_PROMPT as INTERNAL_ENGLISH_SYSTEM_PROMPT
from hl_mem.ingest.extraction.prompts import SYSTEM_PROMPT as INTERNAL_SYSTEM_PROMPT
from hl_mem.ingest.extraction.repair import repair_extraction_json as internal_repair_extraction_json
from hl_mem.ingest.extraction.schema import ExtractionResponseSchema as InternalExtractionResponseSchema
from hl_mem.ingest.llm_extractor import (
    ENGLISH_SYSTEM_PROMPT,
    LLM_EXTRACTOR_VERSION,
    PROMPT_HASH,
    SYSTEM_PROMPT,
    LLMExtractor,
    compute_prompt_hash,
)
from hl_mem.ingest.repair import repair_extraction_json
from hl_mem.ingest.schemas import ExtractionResponseSchema
from hl_mem.llm.types import LLMRequest, LLMResponse


class _Provider:
    name = "phase5-contract"


class _RecordingClient:
    provider = _Provider()
    model = "phase5-contract-model"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse('{"claims":[],"should_memorize":false}', "stop", 0)


@pytest.mark.parametrize(
    ("source", "system_hash", "user_hash"),
    [
        (
            "User prefers tea.",
            "924a97fe6221a732c6fc7e2e333f247635743ba922f26cb258299797c66dcb4d",
            "cb36a6a0e18592a5a89df2af6319e87da7a1b3d6d84b4cdb487c361f1a5d3336",
        ),
        (
            "用户喜欢喝茶。",
            "560ee6c991b284fc10d49967031b299d239b0c49e8939e03e8a62863c546a3b5",
            "74df0124a2336d1613577e753dfacb030ca0fa5e969c5849fc02d4e16b05581a",
        ),
    ],
)
def test_provider_request_payload_is_frozen(source: str, system_hash: str, user_hash: str) -> None:
    client = _RecordingClient()
    extractor = LLMExtractor(client, ChunkingPolicy(10_000, 0, 2))

    assert (
        extractor.extract(
            source,
            {"occurred_at": "2026-08-30T00:00:00+00:00", "context": "fixed"},
        )
        == []
    )

    [request] = client.requests
    assert [(message.role, hashlib.sha256(message.content.encode()).hexdigest()) for message in request.messages] == [
        ("system", system_hash),
        ("user", user_hash),
    ]
    assert request.structured_output is not None
    assert request.structured_output.name == "extraction_response"
    assert request.structured_output.preferred_mode == "json_schema"
    schema = json.dumps(request.structured_output.schema, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(schema.encode()).hexdigest() == (
        "0734cd7c64303b46af49797ec8faccc9b491c1c04ab7b990483fb8bf794c227f"
    )


def test_public_extractor_compatibility_helpers_are_frozen() -> None:
    assert compute_prompt_hash() == PROMPT_HASH
    assert LLM_EXTRACTOR_VERSION == f"llm-v2+{PROMPT_HASH}"
    assert LLMExtractor._parse_json('{"value": 1}') == {"value": 1}
    assert LLMExtractor._schema_error_details(ValueError("invalid"), {}) == [
        {
            "path": "response",
            "error_type": "ValueError",
            "invalid_value": {},
            "allowed_values": ["valid JSON object matching the supplied schema"],
        }
    ]
    claim = LLMExtractor._claim(
        {
            "subject": "user",
            "predicate": "likes",
            "value": "tea",
            "qualifiers": {},
            "confidence": 0.9,
            "volatility": "stable",
        }
    )
    assert LLMExtractor._merge_chunk_claims([[claim], [claim]]) == [claim]


def test_extraction_contract_modules_have_one_canonical_implementation() -> None:
    assert SYSTEM_PROMPT is INTERNAL_SYSTEM_PROMPT
    assert ENGLISH_SYSTEM_PROMPT is INTERNAL_ENGLISH_SYSTEM_PROMPT
    assert ExtractionResponseSchema is InternalExtractionResponseSchema
    assert repair_extraction_json is internal_repair_extraction_json
