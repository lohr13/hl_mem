from typing import Any

import pytest
from pydantic import ValidationError

from hl_mem.ingest.schemas import ExtractionResponseSchema, extraction_response_json_schema


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
    assert ExtractionResponseSchema.model_validate(_valid_response()).claims[0].importance == 0.8


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
    assert schema["$defs"]["ExtractedClaimSchema"]["additionalProperties"] is False
