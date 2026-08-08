"""召回意图路由及双时间可见性领域策略。"""

from __future__ import annotations

from dataclasses import dataclass

from hl_mem.domain.constants import (
    INTENT_KEYWORDS_ANALOGICAL,
    INTENT_KEYWORDS_AS_OF,
    INTENT_KEYWORDS_HISTORICAL,
    INTENT_KEYWORDS_PREFERENCE,
    INTENT_KEYWORDS_PROCEDURAL,
    INTENT_KEYWORDS_RELATIONAL,
    PROCEDURE_ACTION_KEYWORDS,
    PROCEDURE_KEYWORDS,
    TOOL_KEYWORDS,
)
from hl_mem.domain.temporal import RecallIntent

__all__ = ["QueryRoute", "RecallIntent", "route_query", "route_recall_intent"]


@dataclass(frozen=True)
class QueryRoute:
    """召回意图、候选通道和可选参考时间。"""

    intent: str
    channels: tuple[str, ...]
    reference_time: str | None


def route_query(query: str, reference_time: str | None = None) -> QueryRoute:
    """根据中文查询线索选择召回通道。"""
    lowered = query.lower()
    if any(word in lowered for word in INTENT_KEYWORDS_PROCEDURAL):
        return QueryRoute("procedure", ("procedure", "fts", "dense"), reference_time)
    if any(word in lowered for word in INTENT_KEYWORDS_HISTORICAL):
        return QueryRoute("historical", ("temporal", "fact", "fts", "dense"), reference_time)
    if any(word in lowered for word in INTENT_KEYWORDS_RELATIONAL):
        return QueryRoute("relation", ("relation", "fact", "fts", "dense"), reference_time)
    if any(word in lowered for word in INTENT_KEYWORDS_ANALOGICAL):
        return QueryRoute("similar_experience", ("episode", "fts", "dense"), reference_time)
    return QueryRoute("current_state", ("fact", "fts", "dense"), reference_time)


def route_recall_intent(query: str, as_of: str | None, now: str | None = None) -> RecallIntent:
    """根据查询语义推断召回意图；时间快照不改变用户意图。"""
    # 保留 as_of/now 参数以兼容既有调用；可见性与排序时钟由各自管线消费。
    lowered = query.casefold()
    if any(marker in query for marker in (*INTENT_KEYWORDS_AS_OF, "as_of")):
        return RecallIntent.HISTORICAL
    if any(marker in lowered for marker in (*INTENT_KEYWORDS_PREFERENCE, "preference", "prefer", "favorite")):
        return RecallIntent.PREFERENCE
    if any(marker in lowered for marker in TOOL_KEYWORDS):
        return RecallIntent.TOOL
    if "上次" in lowered and any(marker in lowered for marker in PROCEDURE_ACTION_KEYWORDS):
        return RecallIntent.PROCEDURE
    if any(marker in lowered for marker in PROCEDURE_KEYWORDS):
        return RecallIntent.PROCEDURE
    return RecallIntent.CURRENT_STATE
