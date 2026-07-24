"""基于 claim 关系的有界多跳低权重召回扩展。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from hl_mem.domain.relations import get_relations_batch
from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.storage.claims import ClaimRepository


@dataclass(frozen=True)
class RelationExpansionConfig:
    """控制关系扩展的种子、预算、深度和允许关系。"""

    enabled: bool = False
    seed_limit: int = 10
    candidate_limit: int = 20
    relation_weight: float = 0.35
    max_depth: int = 1
    allowed_relations: frozenset[str] = frozenset(
        {"summarizes", "supports", "follows", "about", "derived_from"}
    )


@dataclass(frozen=True)
class RelationHop:
    """描述关系路径中的单次跳转。"""

    from_id: str
    to_id: str
    relation: str
    source: Literal["memory_relations", "evidence_links"]
    edge_confidence: float


@dataclass(frozen=True)
class ExpandedCandidate:
    """记录扩展候选的完整最佳路径与累计权重。"""

    seed_id: str
    candidate_id: str
    path: tuple[RelationHop, ...]
    cumulative_weight: float
    expansion_score: float

    @property
    def claim_id(self) -> str:
        """兼容旧的一跳候选字段名。"""
        return self.candidate_id

    @property
    def relation(self) -> str:
        """返回最后一跳关系，兼容旧调用。"""
        return self.path[-1].relation

    @property
    def source(self) -> Literal["memory_relations", "evidence_links"]:
        """返回最后一跳来源，兼容旧调用。"""
        return self.path[-1].source

    @property
    def edge_confidence(self) -> float:
        """返回最后一跳置信度，兼容旧调用。"""
        return self.path[-1].edge_confidence


@dataclass(frozen=True)
class _Frontier:
    seed_id: str
    current_id: str
    semantic_score: float
    path: tuple[RelationHop, ...]
    cumulative_weight: float


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
    """从融合种子出发执行有界 BFS，并保留每个候选的最高分路径。"""
    if (
        not config.enabled
        or config.seed_limit <= 0
        or config.candidate_limit <= 0
        or config.max_depth <= 0
    ):
        return [], []
    selected_seeds = seeds[: config.seed_limit]
    seed_ids = [str(seed["id"]) for seed in selected_seeds]
    seed_id_set = set(seed_ids)
    frontier = [
        _Frontier(
            seed_id=str(seed["id"]),
            current_id=str(seed["id"]),
            semantic_score=_clamp(seed.get("_semantic_score", seed.get("_score", 0.0))),
            path=(),
            cumulative_weight=1.0,
        )
        for seed in selected_seeds
    ]
    visited = {(item.seed_id, item.current_id) for item in frontier}
    best: dict[str, ExpandedCandidate] = {}
    hop_attenuation = _clamp(config.relation_weight) / 2

    for _depth in range(config.max_depth):
        if not frontier:
            break
        relations = get_relations_batch(
            connection,
            [item.current_id for item in frontier],
            include_memory_relations=True,
            include_reverse_evidence=True,
        )
        next_best: dict[tuple[str, str], _Frontier] = {}
        for item in frontier:
            for edge in relations.get(item.current_id, []):
                neighbor_id = str(edge.get("neighbor_id") or "")
                relation = str(edge.get("relation") or "")
                if (
                    not neighbor_id
                    or neighbor_id in seed_id_set
                    or relation not in config.allowed_relations
                    or (item.seed_id, neighbor_id) in visited
                ):
                    continue
                confidence = _clamp(edge.get("confidence", edge.get("weight", 1.0)))
                hop = RelationHop(
                    from_id=item.current_id,
                    to_id=neighbor_id,
                    relation=relation,
                    source=edge["source"],
                    edge_confidence=confidence,
                )
                cumulative_weight = item.cumulative_weight * confidence * hop_attenuation
                path = (*item.path, hop)
                candidate = ExpandedCandidate(
                    seed_id=item.seed_id,
                    candidate_id=neighbor_id,
                    path=path,
                    cumulative_weight=cumulative_weight,
                    expansion_score=item.semantic_score * cumulative_weight,
                )
                existing = best.get(neighbor_id)
                if existing is None or candidate.expansion_score > existing.expansion_score:
                    best[neighbor_id] = candidate
                next_item = _Frontier(
                    seed_id=item.seed_id,
                    current_id=neighbor_id,
                    semantic_score=item.semantic_score,
                    path=path,
                    cumulative_weight=cumulative_weight,
                )
                next_key = (item.seed_id, neighbor_id)
                previous = next_best.get(next_key)
                if previous is None or cumulative_weight > previous.cumulative_weight:
                    next_best[next_key] = next_item
        visited.update(next_best)
        frontier = sorted(
            next_best.values(),
            key=lambda item: (-item.semantic_score * item.cumulative_weight, item.current_id),
        )[: config.candidate_limit]

    ordered = sorted(best.values(), key=lambda item: (-item.expansion_score, item.candidate_id))
    rows = repo.batch_get_claims([item.candidate_id for item in ordered])
    accepted: list[ExpandedCandidate] = []
    claims: list[dict[str, Any]] = []
    for candidate in ordered:
        claim = rows.get(candidate.candidate_id)
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
