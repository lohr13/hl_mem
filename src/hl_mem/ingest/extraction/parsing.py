"""Pure response parsing and schema diagnostics for LLM extraction."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Literal, Mapping

from pydantic import ValidationError as PydanticValidationError

from hl_mem.domain.claims.attributes import ALLOWED_TOPIC_TAGS
from hl_mem.observability.audit import current_audit


def count_repairs(original: Any, repaired: Any) -> int:
    """Recursively count leaf values changed by deterministic repair."""
    if isinstance(original, dict) and isinstance(repaired, dict):
        return sum(count_repairs(original.get(key), repaired.get(key)) for key in original.keys() | repaired.keys())
    if isinstance(original, list) and isinstance(repaired, list):
        return sum(count_repairs(left, right) for left, right in zip(original, repaired, strict=False)) + abs(
            len(original) - len(repaired)
        )
    return int(original != repaired)


def looks_like_truncated_json(content: str | dict[str, Any]) -> bool:
    """Recognize empty responses and visibly unbalanced JSON containers."""
    if isinstance(content, dict):
        return False
    text = str(content).strip()
    if not text:
        return True
    return (text.startswith("{") and text.count("{") > text.count("}")) or (
        text.startswith("[") and text.count("[") > text.count("]")
    )


def uses_compact_schema(payload: dict[str, Any]) -> bool:
    """Distinguish the compact response from the compatible legacy response."""
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return False
    if not claims:
        return set(payload).issubset({"claims", "should_memorize"})
    compact_markers = {"kind", "notability", "evidence_quote"}
    return any(isinstance(item, dict) and compact_markers.intersection(item) for item in claims)


def parse_legacy_defaults(payload: dict[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Fill versioned fields only for payloads carrying the legacy core signature."""
    compatible = dict(payload)
    claims = compatible.get("claims")
    if not isinstance(claims, list):
        return compatible
    normalized_claims: list[Any] = []
    for item in claims:
        if not isinstance(item, dict):
            normalized_claims.append(item)
            continue
        claim = dict(item)
        legacy_core = {"predicate", "value"}
        versioned_fields = {"canonical_attribute", "scope", "importance"}
        if not legacy_core.issubset(claim) or not versioned_fields.isdisjoint(claim):
            normalized_claims.append(claim)
            continue
        copied_defaults = deepcopy(defaults)
        missing = [key for key in copied_defaults if key not in claim]
        for key in missing:
            claim[key] = copied_defaults[key]
        if missing:
            current_audit().emit(
                "extract",
                "legacy_schema_defaults",
                "applied",
                detail={"fields": missing},
            )
        normalized_claims.append(claim)
    compatible["claims"] = normalized_claims
    return compatible


def schema_error_paths(error: Exception) -> list[str]:
    """Return schema paths safe to include in a retry request."""
    if isinstance(error, PydanticValidationError):
        return [f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}" for item in error.errors()]
    return [f"response:{type(error).__name__}"]


def is_claim_count_overflow(error: BaseException) -> bool:
    """Recognize a claims array that exceeded the response-schema limit."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, PydanticValidationError) and any(
            tuple(item["loc"]) == ("claims",) and item["type"] == "too_long" for item in current.errors()
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def schema_error_details(
    error: Exception,
    payload: Any,
    *,
    kind_values: set[str],
    notability_values: set[str],
) -> list[dict[str, Any]]:
    """Return actionable paths, invalid inputs, and allowed values."""
    if not isinstance(error, PydanticValidationError):
        return [
            {
                "path": "response",
                "error_type": type(error).__name__,
                "invalid_value": payload,
                "allowed_values": ["valid JSON object matching the supplied schema"],
            }
        ]
    details: list[dict[str, Any]] = []
    for item in error.errors():
        path = ".".join(str(part) for part in item["loc"])
        if "topic_tags" in item["loc"]:
            allowed_values: list[str] = sorted(ALLOWED_TOPIC_TAGS)
        elif item["loc"] and item["loc"][-1] == "kind":
            allowed_values = sorted(kind_values)
        elif item["loc"] and item["loc"][-1] == "notability":
            allowed_values = sorted(notability_values)
        elif item["loc"] and item["loc"][-1] == "sensitivity":
            allowed_values = ["normal", "sensitive", "restricted"]
        elif item["loc"] and item["loc"][-1] == "entities":
            allowed_values = ["JSON array of strings", "null (claim entities only)"]
        else:
            allowed_values = [str(item.get("ctx", {}).get("expected", "value matching the JSON schema"))]
        details.append(
            {
                "path": path,
                "error_type": item["type"],
                "invalid_value": item.get("input"),
                "allowed_values": allowed_values,
            }
        )
    return details


def schema_retry_instruction(
    previous_output: Any,
    schema_errors: list[dict[str, Any]],
    language: Literal["zh", "en"] = "zh",
) -> str:
    """Build the bounded correction request for a schema retry."""
    if language == "en":
        return (
            "\nThe previous output did not match the schema. Produce a complete JSON response based on it and "
            "correct only the errors below.\n"
            "<previous_invalid_json>\n"
            f"{json.dumps(previous_output, ensure_ascii=False, default=str)}\n"
            "</previous_invalid_json>\n"
            "<schema_errors>\n"
            f"{json.dumps(schema_errors, ensure_ascii=False, default=str)}\n"
            "</schema_errors>"
        )
    return (
        "\n上一次输出不符合 schema。请基于上次输出生成完整 JSON，只修正下列错误。\n"
        "<previous_invalid_json>\n"
        f"{json.dumps(previous_output, ensure_ascii=False, default=str)}\n"
        "</previous_invalid_json>\n"
        "<schema_errors>\n"
        f"{json.dumps(schema_errors, ensure_ascii=False, default=str)}\n"
        "</schema_errors>"
    )


def parse_json_response(raw: Any) -> dict[str, Any]:
    """Decode a provider response, including a single fenced JSON object."""
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response does not contain valid JSON") from error
        value = json.loads(match.group())
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value
