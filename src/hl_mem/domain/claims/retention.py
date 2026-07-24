"""Claim 保留期计算的纯领域策略。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class TTLPolicy:
    """定义 importance、短期 slot 与 TTL 的映射。"""

    temporal_ttl_days_low: int = 3
    temporal_ttl_days_normal: int = 7
    temporal_ttl_days_high: int = 14
    importance_low_threshold: float = 0.4
    importance_high_threshold: float = 0.7
    importance_write_floor: float = 0.2
    slot_short_ttl_seconds: int = 86400
    short_ttl_slots: frozenset[str] = frozenset({"state.service_health"})


def _parse_iso(value: str, field_name: str) -> datetime:
    """解析 ISO 时间并统一为 UTC；无时区输入按 UTC 解释。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid ISO datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_utc_iso(value: str, field_name: str) -> str:
    """把 ISO 时间规范化为秒精度的 UTC 固定格式。"""
    return _parse_iso(value, field_name).isoformat(timespec="seconds")


def compute_expiration(
    scope: str,
    importance: float,
    volatility: str,
    canonical_slot: str | None,
    valid_to: str | None,
    observed_at: str,
    recorded_from: str,
    policy: TTLPolicy,
) -> tuple[str | None, str]:
    """从原始时间锚点计算绝对过期时间及原因码。"""
    del volatility  # 变化频率不再决定保留期，保留参数用于稳定领域契约。
    anchor_text = observed_at or recorded_from
    anchor = _parse_iso(anchor_text, "observed_at/recorded_from")

    expires_at: datetime | None
    reason: str
    if scope == "permanent":
        expires_at = None
        reason = "permanent"
    elif scope == "temporal":
        if importance < policy.importance_low_threshold:
            ttl = timedelta(days=policy.temporal_ttl_days_low)
            reason = "temporal_low"
        elif importance <= policy.importance_high_threshold:
            ttl = timedelta(days=policy.temporal_ttl_days_normal)
            reason = "temporal_normal"
        else:
            ttl = timedelta(days=policy.temporal_ttl_days_high)
            reason = "temporal_high"
        expires_at = anchor + ttl
    else:
        expires_at = None
        reason = "none"

    if canonical_slot in policy.short_ttl_slots:
        slot_expiration = anchor + timedelta(seconds=policy.slot_short_ttl_seconds)
        if expires_at is None or slot_expiration < expires_at:
            expires_at = slot_expiration
            reason = "slot_short"

    if valid_to:
        valid_to_at = _parse_iso(valid_to, "valid_to")
        if expires_at is None or valid_to_at < expires_at:
            expires_at = valid_to_at
            reason = "valid_to_override"

    normalized_expiration = (
        expires_at.astimezone(timezone.utc).isoformat(timespec="seconds") if expires_at is not None else None
    )
    return normalized_expiration, reason
