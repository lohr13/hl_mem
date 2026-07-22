"""多通道融合、去冗余与上下文预算装箱。"""

from __future__ import annotations

from typing import Any


def reciprocal_rank_fusion(
    channels: list[list[dict[str, Any]]], rank_constant: int = 60
) -> list[dict[str, Any]]:
    """使用 RRF 合并多个有序候选通道。"""
    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}
    for channel in channels:
        for rank, item in enumerate(channel, 1):
            memory_id = str(item["id"])
            items[memory_id] = item
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (rank_constant + rank)
    return sorted(items.values(), key=lambda item: (-scores[str(item["id"])], str(item["id"])))


def budget_pack(items: list[dict[str, Any]], token_budget: int) -> list[dict[str, Any]]:
    """按粗略中文 token 估算将候选顺序装入预算。"""
    if token_budget < 1:
        return []
    packed: list[dict[str, Any]] = []
    used = 0
    for item in items:
        text = str(item.get("text") or item.get("body") or item.get("procedure") or "")
        cost = max(1, (len(text) + 1) // 2)
        if packed and used + cost > token_budget:
            continue
        packed.append(item)
        used += cost
        if used >= token_budget:
            break
    return packed
