"""记忆召回入口、策略匹配与 observation 失效处理。"""

from __future__ import annotations

import re
from typing import Any

from hl_mem.recall.staged_pipeline import hybrid_claims, reciprocal_rank_fusion
from hl_mem.storage.evidence import DerivationRepository

__all__ = [
    "hybrid_claims",
    "matching_policies",
    "reciprocal_rank_fusion",
    "stale_observations",
]


def matching_policies(policies: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """用 trigger 与 query 的通用关键词或短语重叠筛选策略。"""
    normalized_query = query.casefold().strip()
    query_tokens = {token for token in re.findall(r"\w+", normalized_query) if len(token) >= 2}
    matched: list[dict[str, Any]] = []
    for policy in policies:
        trigger = str(policy.get("trigger") or "").casefold().strip()
        trigger_tokens = {token for token in re.findall(r"\w+", trigger) if len(token) >= 2}
        if (
            normalized_query in trigger
            or trigger in normalized_query
            or bool(query_tokens & trigger_tokens)
            or any(token in trigger for token in query_tokens)
            or any(token in normalized_query for token in trigger_tokens)
        ):
            matched.append(policy)
    return matched


def stale_observations(connection: Any, claim_id: str, commit: bool = True) -> None:
    """将依赖指定 claim 的 observation 标记为过期。"""
    rows = connection.execute(
        "SELECT derived_id FROM evidence_links WHERE derived_type='observation' "
        "AND evidence_type='claim' AND evidence_id=?",
        (claim_id,),
    ).fetchall()
    for row in rows:
        DerivationRepository(connection).update_status(row["derived_id"], "stale", commit=commit)
