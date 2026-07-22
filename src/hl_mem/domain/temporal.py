"""双时间可见性领域逻辑。纯函数，不依赖基础设施。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class RecallIntent(StrEnum):
    """召回查询意图。"""

    CURRENT_STATE = "current_state"
    HISTORICAL = "historical"


def parse_utc(value: str) -> datetime:
    """解析 ISO-8601 时间并转换为 UTC；拒绝无时区或无效输入。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid ISO-8601 timestamp without timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _contains(start: str | None, end: str | None, point: datetime) -> bool:
    return (start is None or parse_utc(start) <= point) and (end is None or point < parse_utc(end))


def claim_is_visible(
    claim: dict[str, Any],
    valid_as_of: str,
    known_as_of: str | None,
    intent: RecallIntent | str,
) -> bool:
    """按状态、有效时间与记录时间判断 claim 是否可见。"""
    selected_intent = RecallIntent(intent)
    valid_point = parse_utc(valid_as_of)
    if not _contains(claim.get("valid_from"), claim.get("valid_to"), valid_point):
        return False
    if known_as_of and not _contains(claim.get("recorded_from"), claim.get("recorded_to"), parse_utc(known_as_of)):
        return False
    if selected_intent is RecallIntent.CURRENT_STATE:
        if claim.get("status", "active") != "active":
            return False
        expires_at = claim.get("expires_at")
        return not expires_at or parse_utc(expires_at) > valid_point
    return claim.get("status", "active") in {"active", "superseded", "expired"}
