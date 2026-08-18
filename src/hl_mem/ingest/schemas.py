"""LLM 记忆提取响应的严格 Pydantic schema。"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Any, Literal, TypeAlias, get_args

from pydantic import BaseModel, ConfigDict, Field

from hl_mem.domain.claims.attributes import ALLOWED_TOPIC_TAGS, OPERATIONAL_SLOT_NAMES

from .extractors import AssertionKind

CanonicalSlot: TypeAlias = Literal[
    "preference.ui_theme",
    "preference.response_style",
    "preference.tool_choice",
    "choice.tool",
    "choice.database",
    "choice.model",
    "choice.provider",
    "choice.memory_system",
    "state.service_health",
    "identity.name",
    "config.port",
    "config.path",
    "config.env",
    "config.network",
    "plan.deadline",
]
TopicTag: TypeAlias = Literal[
    "account",
    "api",
    "architecture",
    "behavior",
    "bugfix",
    "capability",
    "cause",
    "choice",
    "config",
    "connectivity",
    "constraint",
    "contact",
    "decision",
    "dependency",
    "deployment",
    "evaluation",
    "fact",
    "framework",
    "goal",
    "hardware",
    "identity",
    "implementation",
    "issue",
    "job",
    "membership",
    "memory",
    "migration",
    "os",
    "other",
    "plan",
    "preference",
    "process",
    "protocol",
    "requirement",
    "resolution",
    "role",
    "routing",
    "schedule",
    "state",
    "test",
    "timeout",
    "tool_choice",
    "version",
    "workflow",
]

if set(get_args(CanonicalSlot)) != set(OPERATIONAL_SLOT_NAMES):
    raise RuntimeError("CanonicalSlot type alias is out of sync with OPERATIONAL_SLOT_NAMES")
if set(get_args(TopicTag)) != set(ALLOWED_TOPIC_TAGS):
    raise RuntimeError("TopicTag type alias is out of sync with ALLOWED_TOPIC_TAGS")


class ExtractedClaimSchema(BaseModel):
    """单条 LLM 提取事实的结构契约。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    canonical_attribute: str = Field(min_length=1)
    canonical_slot: CanonicalSlot | None = None
    topic_tags: list[TopicTag] = Field(default_factory=list)
    value: str = Field(min_length=1)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    volatility: Literal["stable", "ephemeral"]
    reason: str = ""
    scope: Literal["temporal", "permanent"]
    importance: float = Field(ge=0.0, le=1.0)
    occurred_start: str | None = None
    occurred_end: str | None = None
    entities: list[str] | None = None
    assertion_kind: AssertionKind = "unknown"
    source_event_indices: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: [0],
        min_length=1,
        max_length=32,
    )


class ExtractionResponseSchema(BaseModel):
    """完整 LLM 提取响应的结构契约。"""

    model_config = ConfigDict(extra="forbid")

    claims: list[ExtractedClaimSchema]
    entities: list[str] = Field(default_factory=list)
    should_memorize: bool
    sensitivity: Literal["normal", "sensitive", "restricted"] = "normal"


class CompactExtractedClaimSchema(BaseModel):
    """LLM 直接输出的最小候选契约。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1)
    action: str | None = Field(min_length=1, max_length=100)
    object: str | None = Field(min_length=1, max_length=500)
    kind: Literal["preference", "architecture", "identity", "config", "fact", "plan", "choice"]
    confidence: float = Field(ge=0.0, le=1.0)
    notability: Literal["high", "medium", "low"]
    assertion_kind: AssertionKind = "unknown"
    evidence_quote: str = Field(min_length=1)
    source_event_indices: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=lambda: [0],
        min_length=1,
        max_length=32,
    )


class CompactExtractionResponseSchema(BaseModel):
    """供结构化输出使用的紧凑提取响应。"""

    model_config = ConfigDict(extra="forbid")

    claims: list[CompactExtractedClaimSchema] = Field(max_length=20)
    should_memorize: bool


def extraction_response_json_schema() -> dict[str, Any]:
    """生成紧凑、递归 additionalProperties=false 的远端 JSON Schema。"""
    return CompactExtractionResponseSchema.model_json_schema()


def legacy_extraction_response_json_schema() -> dict[str, Any]:
    """Project the frozen seven-field compact request contract from the parser schema."""
    schema = deepcopy(extraction_response_json_schema())
    claim = schema["$defs"]["CompactExtractedClaimSchema"]
    for field in ("action", "object", "assertion_kind"):
        claim["properties"].pop(field)
        if field in claim["required"]:
            claim["required"].remove(field)
    return schema


def source_bounded_rao_extraction_response_json_schema() -> dict[str, Any]:
    """Keep the frozen RAO evaluation contract independent from the A1 gate."""
    schema = deepcopy(extraction_response_json_schema())
    claim = schema["$defs"]["CompactExtractedClaimSchema"]
    claim["properties"].pop("assertion_kind")
    if "assertion_kind" in claim["required"]:
        claim["required"].remove("assertion_kind")
    return schema


def temporal_gate_extraction_response_json_schema() -> dict[str, Any]:
    """Project the product eight-field contract with a required epistemic gate."""
    schema = deepcopy(extraction_response_json_schema())
    claim = schema["$defs"]["CompactExtractedClaimSchema"]
    for field in ("action", "object"):
        claim["properties"].pop(field)
        claim["required"].remove(field)
    if "assertion_kind" not in claim["required"]:
        claim["required"].append("assertion_kind")
    return schema
