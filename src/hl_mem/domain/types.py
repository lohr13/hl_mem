"""HL-Mem 跨层使用的稳定领域数据类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class StoredEvent:
    """已持久化的事件。"""

    id: str
    source: str
    content: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ClaimDraft:
    """等待持久化的声明草稿。"""

    predicate: str
    canonical_attribute: str
    value: str
    scope: str
    importance: float
    qualifiers: dict[str, Any]
    evidence: dict[str, Any] | None


@dataclass(frozen=True)
class StoredClaim:
    """已持久化的声明。"""

    id: str
    entity_id: str
    predicate: str
    canonical_attribute: str
    value: str
    scope: str
    importance: float
    status: str
    qualifiers: dict[str, Any]
    created_at: str
    valid_from: str
    valid_until: str | None


@dataclass(frozen=True)
class RecallResult:
    """带召回分数和来源的声明。"""

    claim: StoredClaim
    score: float
    source: Literal["fts", "vector", "related"]


@dataclass(frozen=True)
class FeedbackRecord:
    """声明的显式反馈记录。"""

    claim_id: str
    feedback_type: str
    weight: float
    created_at: str
