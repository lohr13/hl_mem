"""LLM 记忆提取响应的严格 Pydantic schema。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hl_mem.domain.claims.attributes import ALLOWED_TOPIC_TAGS, OPERATIONAL_SLOT_NAMES

CanonicalSlot = Literal[*OPERATIONAL_SLOT_NAMES]
TopicTag = Literal[*tuple(sorted(ALLOWED_TOPIC_TAGS))]


class ExtractedClaimSchema(BaseModel):
    """单条 LLM 提取事实的结构契约。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    canonical_attribute: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    canonical_slot: CanonicalSlot | None = None
    topic_tags: list[TopicTag] = Field(default_factory=list)
    value: str = Field(min_length=1)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    volatility: Literal["stable", "ephemeral"]
    reason: str = ""
    scope: Literal["temporal", "permanent"]
    importance: float = Field(ge=0.0, le=1.0)


class ExtractionResponseSchema(BaseModel):
    """完整 LLM 提取响应的结构契约。"""

    model_config = ConfigDict(extra="forbid")

    claims: list[ExtractedClaimSchema]
    entities: list[str] = Field(default_factory=list)
    should_memorize: bool
    sensitivity: Literal["normal", "sensitive", "restricted"] = "normal"


def extraction_response_json_schema() -> dict[str, Any]:
    """生成保留递归 additionalProperties=false 的远端 JSON Schema。"""
    return ExtractionResponseSchema.model_json_schema()
