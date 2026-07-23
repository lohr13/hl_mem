"""召回意图路由及双时间可见性策略的兼容入口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from hl_mem.domain.temporal import RecallIntent, claim_is_visible, parse_utc  # re-export for backward compatibility


@dataclass(frozen=True)
class QueryRoute:
    """召回意图、候选通道和可选参考时间。"""

    intent: str
    channels: tuple[str, ...]
    reference_time: str | None


def route_query(query: str, reference_time: str | None = None) -> QueryRoute:
    """根据中文查询线索选择召回通道。"""
    lowered = query.lower()
    if any(word in lowered for word in ("如何", "怎么", "步骤", "流程", "部署")):
        return QueryRoute("procedure", ("procedure", "fts", "dense"), reference_time)
    if any(word in lowered for word in ("去年", "以前", "历史", "当时", "曾经")) or reference_time:
        return QueryRoute("historical", ("temporal", "fact", "fts", "dense"), reference_time)
    if any(word in lowered for word in ("关系", "关联", "依赖", "属于")):
        return QueryRoute("relation", ("relation", "fact", "fts", "dense"), reference_time)
    if any(word in lowered for word in ("类似", "经验", "上次")):
        return QueryRoute("similar_experience", ("episode", "fts", "dense"), reference_time)
    return QueryRoute("current_state", ("fact", "fts", "dense"), reference_time)


def route_recall_intent(query: str, as_of: str | None, now: str | None = None) -> RecallIntent:
    """根据显式历史措辞或过去的 as_of 推断召回意图。"""
    if any(marker in query for marker in ("当时", "以前", "历史", "曾经", "截至", "as_of")):
        return RecallIntent.HISTORICAL
    if as_of is not None:
        reference = parse_utc(now) if now else datetime.now(timezone.utc)
        if parse_utc(as_of) < reference:
            return RecallIntent.HISTORICAL
    return RecallIntent.CURRENT_STATE
