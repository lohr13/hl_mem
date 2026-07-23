"""基于 claim 关系的一跳低权重召回扩展。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from hl_mem.domain.relations import get_relations_batch
from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.storage.repository import ClaimRepository


@dataclass(frozen=True)
class RelationExpansionConfig:
    """控制一跳关系扩展的种子、预算和允许关系。"""

    enabled: bool = False
    seed_limit: int = 10
    candidate_limit: int = 20
    relation_weight: float = 0.35
    allowed_relations: frozenset[str] = frozenset(
        {"summarizes", "supports", "follows", "about", "derived_from"}
    )


@dataclass(frozen=True)
class ExpandedCandidate:
    """记录扩展候选的最佳一跳证据路径。"""

    claim_id: str
    seed_id: str
    relation: str
    source: Literal["memory_relations", "evidence_links"]
    edge_confidence: float
    expansion_score: float


def _clamp(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def expand_related_claims(
    connection: sqlite3.Connection,
    repo: ClaimRepository,
    seeds: list[dict[str, Any]],
    reference: str,
    known_as_of: str | None,
    intent: RecallIntent,
    namespace: str,
    config: RelationExpansionConfig,
) -> tuple[list[dict[str, Any]], list[ExpandedCandidate]]:
    """从融合种子出发扩展一跳可见 claim，并为每个候选保留最高分路径。"""
    if not config.enabled or config.seed_limit <= 0 or config.candidate_limit <= 0:
        return [], []
    selected_seeds = seeds[: config.seed_limit]
    seed_ids = [str(seed["id"]) for seed in selected_seeds]
    seed_id_set = set(seed_ids)
    relations = get_relations_batch(
        connection,
        seed_ids,
        include_memory_relations=True,
        include_reverse_evidence=True,
    )
    best: dict[str, ExpandedCandidate] = {}
    for seed in selected_seeds:
        seed_id = str(seed["id"])
        semantic = _clamp(seed.get("_semantic_score", seed.get("_score", 0.0)))
        for edge in relations.get(seed_id, []):
            neighbor_id = edge.get("neighbor_id")
            relation = str(edge.get("relation") or "")
            if not neighbor_id or neighbor_id in seed_id_set or relation not in config.allowed_relations:
                continue
            confidence = _clamp(edge.get("confidence", edge.get("weight", 1.0)))
            score = semantic * confidence * _clamp(config.relation_weight) / 2
            candidate = ExpandedCandidate(
                claim_id=str(neighbor_id),
                seed_id=seed_id,
                relation=relation,
                source=edge["source"],
                edge_confidence=confidence,
                expansion_score=score,
            )
            existing = best.get(candidate.claim_id)
            if existing is None or candidate.expansion_score > existing.expansion_score:
                best[candidate.claim_id] = candidate
    ordered = sorted(best.values(), key=lambda item: (-item.expansion_score, item.claim_id))
    rows = repo.batch_get_claims([item.claim_id for item in ordered])
    accepted: list[ExpandedCandidate] = []
    claims: list[dict[str, Any]] = []
    for candidate in ordered:
        claim = rows.get(candidate.claim_id)
        if (
            claim is None
            or claim.get("namespace_key") != namespace
            or not claim_is_visible(claim, reference, known_as_of, intent)
        ):
            continue
        expanded = dict(claim)
        expanded["_semantic_score"] = candidate.expansion_score
        claims.append(expanded)
        accepted.append(candidate)
        if len(claims) >= config.candidate_limit:
            break
    return claims, accepted
