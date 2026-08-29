from typing import Any

import pytest
from pydantic import ValidationError

from hl_mem.domain.claims.attributes import validate_canonical_attribute
from hl_mem.ingest.schemas import (
    CompactExtractionResponseSchema,
    ExtractionResponseSchema,
    extraction_response_json_schema,
    legacy_extraction_response_json_schema,
)


def _valid_response() -> dict[str, Any]:
    return {
        "claims": [
            {
                "subject": "用户",
                "predicate": "偏好",
                "canonical_attribute": "preference.ui_theme",
                "value": "深色模式",
                "qualifiers": {},
                "confidence": 0.9,
                "volatility": "stable",
                "reason": "明确陈述",
                "scope": "permanent",
                "importance": 0.8,
            }
        ],
        "entities": ["用户"],
        "should_memorize": True,
        "sensitivity": "normal",
    }


def test_valid_extraction_response_is_accepted() -> None:
    claim = ExtractionResponseSchema.model_validate(_valid_response()).claims[0]
    assert claim.importance == 0.8
    assert claim.assertion_kind == "unknown"


def test_assertion_kind_is_a_restricted_epistemic_gate() -> None:
    payload = _valid_response()
    payload["claims"][0]["assertion_kind"] = "observation"
    assert ExtractionResponseSchema.model_validate(payload).claims[0].assertion_kind == "observation"

    payload["claims"][0]["assertion_kind"] = "level"
    with pytest.raises(ValidationError):
        ExtractionResponseSchema.model_validate(payload)


def test_noncanonical_attribute_reaches_domain_fallback() -> None:
    payload = _valid_response()
    payload["claims"][0]["canonical_attribute"] = "中文属性"

    claim = ExtractionResponseSchema.model_validate(payload).claims[0]

    assert validate_canonical_attribute(claim.predicate, claim.canonical_attribute) == "custom.unknown"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unexpected",), True),
        (("claims", 0, "confidence"), 1.1),
        (("claims", 0, "volatility"), "unknown"),
        (("claims",), {}),
    ],
)
def test_invalid_extraction_response_is_rejected(path: tuple[Any, ...], value: Any) -> None:
    payload = _valid_response()
    target: Any = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ExtractionResponseSchema.model_validate(payload)


def test_generated_schema_forbids_extra_fields_recursively() -> None:
    schema = extraction_response_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["CompactExtractedClaimSchema"]["additionalProperties"] is False
    assert (
        CompactExtractionResponseSchema.model_validate(
            {
                "claims": [
                    {
                        "subject": "用户",
                        "value": "用户偏好简洁回答",
                        "action": None,
                        "object": None,
                        "kind": "preference",
                        "confidence": 1.0,
                        "notability": "high",
                        "evidence_quote": "偏好简洁回答",
                    }
                ],
                "should_memorize": True,
            }
        )
        .claims[0]
        .kind
        == "preference"
    )


def test_compact_schema_accepts_at_most_30_claims() -> None:
    claim = {
        "subject": "用户",
        "value": "用户记录了一条可回答事实",
        "action": None,
        "object": None,
        "kind": "fact",
        "confidence": 1.0,
        "notability": "medium",
        "evidence_quote": "记录了一条可回答事实",
    }
    payload = {"claims": [claim.copy() for _ in range(30)], "should_memorize": True}

    assert len(CompactExtractionResponseSchema.model_validate(payload).claims) == 30
    assert extraction_response_json_schema()["properties"]["claims"]["maxItems"] == 30

    payload["claims"].append(claim.copy())
    with pytest.raises(ValidationError):
        CompactExtractionResponseSchema.model_validate(payload)


def test_compact_schema_exposes_gate_without_changing_frozen_legacy_contract() -> None:
    current_claim = extraction_response_json_schema()["$defs"]["CompactExtractedClaimSchema"]
    legacy_claim = legacy_extraction_response_json_schema()["$defs"]["CompactExtractedClaimSchema"]

    assert current_claim["properties"]["assertion_kind"]["default"] == "unknown"
    assert "assertion_kind" not in legacy_claim["properties"]


def test_compact_relation_fields_are_required_and_nullable() -> None:
    schema = extraction_response_json_schema()
    compact_claim = schema["$defs"]["CompactExtractedClaimSchema"]

    assert {"action", "object"}.issubset(compact_claim["properties"])
    assert {"action", "object"}.issubset(compact_claim["required"])
    payload = {
        "claims": [
            {
                "subject": "用户",
                "value": "用户参加 Emily 的婚礼",
                "action": None,
                "object": None,
                "kind": "fact",
                "confidence": 1.0,
                "notability": "low",
                "evidence_quote": "参加 Emily 的婚礼",
            }
        ],
        "should_memorize": True,
    }
    claim = CompactExtractionResponseSchema.model_validate(payload).claims[0]
    assert claim.action is None
    assert claim.object is None


def test_source_event_indices_match_configurable_microbatch_ceiling() -> None:
    payload = _valid_response()
    payload["claims"][0]["source_event_indices"] = list(range(32))

    assert len(ExtractionResponseSchema.model_validate(payload).claims[0].source_event_indices) == 32
    compact_claim = extraction_response_json_schema()["$defs"]["CompactExtractedClaimSchema"]
    assert compact_claim["properties"]["source_event_indices"]["maxItems"] == 32

    payload["claims"][0]["source_event_indices"] = list(range(33))
    with pytest.raises(ValidationError):
        ExtractionResponseSchema.model_validate(payload)
