"""有序向量候选的批量回表与权威可见性过滤。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from hl_mem.domain.temporal import RecallIntent, claim_is_visible


class CandidateRepository(Protocol):
    """候选物化所需的最小 Claim 仓储接口。"""

    def batch_get_claims(self, claim_ids: list[str]) -> dict[str, dict[str, Any]]: ...


def materialize_candidates(
    repo: CandidateRepository,
    scored_ids: list[tuple[str, float]],
    limit: int,
    reference: str,
    known_as_of: str | None,
    selected_intent: RecallIntent,
    claim_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """从有序候选循环回表、过滤，并在达到 limit 时截断。"""
    if limit <= 0:
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    batch_size = max(limit * 3, limit + 50)
    while len(results) < limit and offset < len(scored_ids):
        batch = scored_ids[offset : offset + batch_size]
        offset += len(batch)
        claims_by_id = repo.batch_get_claims([claim_id for claim_id, _score in batch if claim_id not in seen])
        for claim_id, score in batch:
            if claim_id in seen:
                continue
            seen.add(claim_id)
            claim = claims_by_id.get(claim_id)
            if (
                claim is None
                or (claim_filter is not None and not claim_filter(claim))
                or not claim_is_visible(claim, reference, known_as_of, selected_intent)
            ):
                continue
            results.append({**claim, "_score": score})
            if len(results) >= limit:
                break
    return results
