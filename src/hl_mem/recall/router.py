"""确定性查询路由器。"""

from __future__ import annotations

from dataclasses import dataclass


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
