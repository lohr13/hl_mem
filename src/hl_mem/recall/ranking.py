"""召回候选的多因子特征、先验评分与重排融合。"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_WEIGHTS = {
    "semantic": 0.65,
    "recency": 0.08,
    "access_frequency": 0.07,
    "confidence": 0.075,
    "importance": 0.075,
    "utility": 0.05,
}


def decay_ranking_weights(decay_model: str) -> dict[str, float]:
    """Map each decay arm onto the same frozen epistemic/activity weight."""

    if decay_model not in {"legacy_linear", "activation_halflife", "confidence_halflife"}:
        raise ValueError(f"unsupported decay model: {decay_model}")
    weights = dict(DEFAULT_WEIGHTS)
    if decay_model == "activation_halflife":
        weights["activation"] = weights.pop("confidence")
    return weights


def _clamp(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def memory_features(
    claim: Mapping[str, Any], semantic_score: float, max_access_count: int, now: str
) -> dict[str, float]:
    """把 Claim 转换为归一化的语义、时效、访问和质量特征。"""
    current = _parse_datetime(now) or datetime.now(timezone.utc)
    observed = (
        _parse_datetime(claim.get("observed_at"))
        or _parse_datetime(claim.get("occurred_start"))
        or _parse_datetime(claim.get("valid_from"))
        or _parse_datetime(claim.get("recorded_from"))
    )
    recency = 0.0
    if observed is not None:
        age_days = max(0.0, (current - observed).total_seconds() / 86400.0)
        recency = 1.0 / (1.0 + age_days / 30.0)
    try:
        accesses = max(0, int(claim.get("access_count", 0)))
    except (TypeError, ValueError):
        accesses = 0
    access_frequency = math.log1p(accesses) / math.log1p(max_access_count) if max_access_count > 0 else 0.0
    return {
        "semantic": _clamp(semantic_score),
        "recency": _clamp(recency),
        "access_frequency": _clamp(access_frequency),
        "confidence": _clamp(claim.get("confidence", 0.5)),
        "activation": _clamp(claim.get("activation", 1.0)),
        "importance": _clamp(claim.get("importance", 0.5)),
        "utility": _clamp(claim.get("helpful_rate", 0.5)),
    }


def memory_score(features: Mapping[str, float], weights: Mapping[str, float] = DEFAULT_WEIGHTS) -> float:
    """按冻结权重计算候选的多因子先验分数。"""
    return sum(float(weight) * _clamp(features.get(name, 0.0)) for name, weight in weights.items())


def blend_reranker_score(
    reranker_score: float,
    features: Mapping[str, float],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> float:
    """融合远程重排相关度与非语义先验，生成最终排序分数。"""
    prior_weights = {name: weight for name, weight in weights.items() if name != "semantic"}
    prior_total = sum(prior_weights.values())
    prior = (
        sum(weight * _clamp(features.get(name, 0.0)) for name, weight in prior_weights.items()) / prior_total
        if prior_total > 0.0
        else 0.0
    )
    return 0.80 * _clamp(reranker_score) + 0.20 * prior
