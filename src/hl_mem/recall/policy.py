"""召回意图路由及双时间可见性策略的兼容入口。"""

from __future__ import annotations

from datetime import datetime, timezone

from hl_mem.domain.temporal import RecallIntent, claim_is_visible, parse_utc  # re-export for backward compatibility


def route_recall_intent(query: str, as_of: str | None, now: str | None = None) -> RecallIntent:
    """根据显式历史措辞或过去的 as_of 推断召回意图。"""
    if any(marker in query for marker in ("当时", "以前", "历史", "曾经", "截至", "as_of")):
        return RecallIntent.HISTORICAL
    if as_of is not None:
        reference = parse_utc(now) if now else datetime.now(timezone.utc)
        if parse_utc(as_of) < reference:
            return RecallIntent.HISTORICAL
    return RecallIntent.CURRENT_STATE
