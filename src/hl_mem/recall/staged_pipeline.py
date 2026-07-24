"""分阶段的混合召回、排序、关系扩展与收尾实现。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hl_mem.config import RECALL_VECTOR_SCAN_LIMIT
from hl_mem.domain.claims.attributes import SLOT_REGISTRY, normalize_predicate
from hl_mem.domain.claims.query_tags import (
    LOW_INFORMATION_TAGS,
    TAG_INFO_WEIGHT,
    extract_query_tags,
)
from hl_mem.domain.recall import RecallIntent, claim_is_visible, route_recall_intent
from hl_mem.observability.audit import current_audit
from hl_mem.recall.ranking import DEFAULT_WEIGHTS, blend_reranker_score, memory_features, memory_score
from hl_mem.recall.relation_expansion import RelationExpansionConfig, expand_related_claims
from hl_mem.recall.reranker import DashScopeReranker, RerankResult
from hl_mem.recall.trace import SearchTracer
from hl_mem.storage.claims import ClaimRepository

# ── 排序因子冻结 ──────────────────────────────────────────────
# 排序链已稳定，不再增加新 boost/channel/weight。
# 新增召回能力应通过已有通道（FTS/Dense/Tag）的参数调优实现，
# 而非引入新的排序因子。如需新增，必须先建立离线评测集并证明不退化。
# ──────────────────────────────────────────────────────────────

RRF_K = 60


@dataclass(frozen=True)
class RecallConfig:
    """召回管线使用的完整排序配置。"""

    candidate_floor: int = 50
    tag_boost_enabled: bool = True
    tag_boost_weight: float = 0.05
    tag_channel_enabled: bool = False
    tag_channel_weight: float = 0.15
    tag_candidate_limit: int = 20
    preference_recency_boost: float = 1.0


@dataclass
class RecallContext:
    """召回管线各阶段的共享上下文。"""

    repo: ClaimRepository
    query: str = ""
    query_blob: bytes = b""
    limit: int = 5
    as_of: str | None = None
    reranker: DashScopeReranker | None = None
    known_as_of: str | None = None
    namespace: str = "default"
    relation_connection: sqlite3.Connection | None = None
    relation_config: RelationExpansionConfig | None = None
    tracer: SearchTracer | None = None

    candidate_limit: int = 50
    ranking_now: str = ""
    selected_intent: RecallIntent | None = None
    reference: str = ""
    preference_boost: float = 1.0
    query_tags: list[str] = field(default_factory=list)
    tag_boost_enabled: bool = True
    tag_boost_weight: float = 0.05
    tag_channel_enabled: bool = False
    tag_channel_weight: float = 0.15
    tag_candidate_limit: int = 20
    fts: list[dict[str, Any]] = field(default_factory=list)
    dense: list[dict[str, Any]] = field(default_factory=list)
    tags: list[dict[str, Any]] = field(default_factory=list)
    fts_us: int = 0
    dense_us: int = 0
    tag_us: int = 0
    total_started: int = 0

    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    feature_by_id: dict[str, dict[str, float]] = field(default_factory=dict)
    pre_scores: dict[str, float] = field(default_factory=dict)
    tag_boosts: dict[str, float] = field(default_factory=dict)
    ranked_claims: list[dict[str, Any]] = field(default_factory=list)

    rerank_us: int = 0
    reranked: list = field(default_factory=list)
    valid_reranked: list = field(default_factory=list)
    rerank_scores: dict[str, float] = field(default_factory=dict)
    ranked_result: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = ""


def _claim_text(claim: dict[str, Any]) -> str:
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value', '')}"


def _recorded_epoch(claim: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(claim.get("recorded_from") or "")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _access_count(claim: dict[str, Any]) -> int:
    try:
        return max(0, int(claim.get("access_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _is_preference_claim(claim: dict[str, Any]) -> bool:
    if normalize_predicate(str(claim.get("predicate") or "")) == "偏好":
        return True
    definition = SLOT_REGISTRY.get(str(claim.get("canonical_slot") or ""))
    if definition is not None and definition.predicate == "偏好":
        return True
    legacy_definition = SLOT_REGISTRY.get(str(claim.get("canonical_attribute") or ""))
    return legacy_definition is not None and legacy_definition.predicate == "偏好"


def _visibility_filter_reason(
    claim: dict[str, Any],
    reference: str,
    known_as_of: str | None,
    selected_intent: RecallIntent,
) -> str:
    if known_as_of and claim_is_visible(claim, reference, None, selected_intent):
        return "not_visible_recorded_time"
    active_claim = {**claim, "status": "active"}
    if claim.get("status", "active") != "active" and claim_is_visible(
        active_claim, reference, known_as_of, selected_intent
    ):
        return "status_filtered"
    return "not_visible_valid_time"


def _preference_first(claims: list[dict[str, Any]], limit: int, selected_intent: RecallIntent) -> list[dict[str, Any]]:
    if selected_intent is not RecallIntent.PREFERENCE:
        return claims[:limit]
    preferences = [claim for claim in claims if _is_preference_claim(claim)]
    others = [claim for claim in claims if not _is_preference_claim(claim)]
    reserved = min(3, limit, len(preferences))
    return (preferences[:reserved] + preferences[reserved:] + others)[:limit]


def _rrf_scores(channels: list[list[dict[str, Any]]], rank_constant: int) -> dict[str, float]:
    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    scores: dict[str, float] = {}
    for channel in channels:
        for rank, item in enumerate(channel, 1):
            item_id = str(item["id"])
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (rank_constant + rank)
    return scores


def _weighted_rrf_scores(
    channels: list[tuple[list[dict[str, Any]], float]],
    rank_constant: int,
) -> dict[str, float]:
    """按通道权重计算 RRF，空通道不产生分数。"""
    scores: dict[str, float] = {}
    for channel, weight in channels:
        for rank, item in enumerate(channel, 1):
            item_id = str(item["id"])
            scores[item_id] = scores.get(item_id, 0.0) + weight / (rank_constant + rank)
    return scores


def reciprocal_rank_fusion(channels: list[list[dict[str, Any]]], rank_constant: int = RRF_K) -> list[dict[str, Any]]:
    """使用唯一的 RRF 实现合并多个有序候选通道。"""
    scores = _rrf_scores(channels, rank_constant)
    items = {str(item["id"]): item for channel in channels for item in channel}
    return sorted(items.values(), key=lambda item: (-scores[str(item["id"])], str(item["id"])))


def hybrid_claims(
    repo: ClaimRepository,
    query: str,
    query_blob: bytes,
    limit: int,
    as_of: str | None,
    reranker: DashScopeReranker | None = None,
    now: str | None = None,
    intent: RecallIntent | str | None = None,
    known_as_of: str | None = None,
    namespace: str = "default",
    *,
    recall_config: RecallConfig | None = None,
    relation_connection: sqlite3.Connection | None = None,
    relation_config: RelationExpansionConfig | None = None,
    tracer: SearchTracer | None = None,
    candidate_floor: int | None = None,
    preference_recency_boost: float | None = None,
    tag_boost_enabled: bool | None = None,
    tag_boost_weight: float | None = None,
    tag_channel_enabled: bool | None = None,
    tag_channel_weight: float | None = None,
    tag_candidate_limit: int | None = None,
) -> list[dict[str, Any]]:
    """协调候选收集、过滤评分、关系扩展、重排和结果收尾。"""
    state = _collect_candidates(
        repo,
        query,
        query_blob,
        limit,
        as_of,
        reranker,
        now,
        intent,
        known_as_of,
        namespace,
        recall_config=recall_config,
        relation_connection=relation_connection,
        relation_config=relation_config,
        tracer=tracer,
        candidate_floor=candidate_floor,
        preference_recency_boost=preference_recency_boost,
        tag_boost_enabled=tag_boost_enabled,
        tag_boost_weight=tag_boost_weight,
        tag_channel_enabled=tag_channel_enabled,
        tag_channel_weight=tag_channel_weight,
        tag_candidate_limit=tag_candidate_limit,
    )
    return _finalize(_rerank(_expand_related(_filter_and_score(state))))


def _collect_candidates(
    repo: ClaimRepository,
    query: str,
    query_blob: bytes,
    limit: int,
    as_of: str | None,
    reranker: DashScopeReranker | None = None,
    now: str | None = None,
    intent: RecallIntent | str | None = None,
    known_as_of: str | None = None,
    namespace: str = "default",
    *,
    recall_config: RecallConfig | None = None,
    relation_connection: sqlite3.Connection | None = None,
    relation_config: RelationExpansionConfig | None = None,
    tracer: SearchTracer | None = None,
    candidate_floor: int | None = None,
    preference_recency_boost: float | None = None,
    tag_boost_enabled: bool | None = None,
    tag_boost_weight: float | None = None,
    tag_channel_enabled: bool | None = None,
    tag_channel_weight: float | None = None,
    tag_candidate_limit: int | None = None,
) -> RecallContext:
    """仅执行 FTS 与向量检索，并建立统一时间快照。"""
    config = recall_config or RecallConfig()
    effective_floor = candidate_floor or config.candidate_floor
    candidate_limit = min(RECALL_VECTOR_SCAN_LIMIT, max(limit * 5, effective_floor))
    ranking_now = now or datetime.now(timezone.utc).isoformat()
    selected_intent = RecallIntent(intent) if intent else route_recall_intent(query, as_of, ranking_now)
    reference = as_of or ranking_now
    total_started = time.perf_counter_ns()
    effective_tag_boost_enabled = config.tag_boost_enabled if tag_boost_enabled is None else tag_boost_enabled
    effective_tag_channel_enabled = config.tag_channel_enabled if tag_channel_enabled is None else tag_channel_enabled
    query_tags = (
        extract_query_tags(query)
        if effective_tag_boost_enabled or effective_tag_channel_enabled
        else []
    )

    started = time.perf_counter_ns()
    fts = repo.search_claims_fts(query, candidate_limit, reference, selected_intent, known_as_of, namespace=namespace)
    fts_us = (time.perf_counter_ns() - started) // 1000
    if tracer is not None:
        tracer.trace.candidate_limit = candidate_limit
        tracer.trace.phases.fts_us = fts_us
        tracer.record_channel("fts", fts)

    started = time.perf_counter_ns()
    dense = repo.search_claims_vector(
        query_blob, candidate_limit, reference, selected_intent, known_as_of, namespace=namespace
    )
    dense_us = (time.perf_counter_ns() - started) // 1000
    if tracer is not None:
        tracer.trace.phases.dense_us = dense_us
        tracer.record_channel("dense", dense)

    effective_tag_candidate_limit = tag_candidate_limit or config.tag_candidate_limit
    tag_results: list[dict[str, Any]] = []
    tag_us = 0
    if effective_tag_channel_enabled and query_tags:
        started = time.perf_counter_ns()
        tag_results = repo.search_claims_tags(
            query_tags,
            namespace,
            effective_tag_candidate_limit,
            reference,
            selected_intent,
            known_as_of,
        )
        tag_us = (time.perf_counter_ns() - started) // 1000
        if tracer is not None:
            tracer.trace.phases.tag_us = tag_us
            tracer.record_channel("tag", tag_results)
    if tracer is not None:
        tracer.trace.query_tags = query_tags
        tracer.trace.tag_boost_applied = bool(effective_tag_boost_enabled and query_tags)
        tracer.trace.tag_channel_applied = bool(effective_tag_channel_enabled and query_tags and tag_results)

    return RecallContext(
        repo=repo,
        query=query,
        query_blob=query_blob,
        limit=limit,
        as_of=as_of,
        reranker=reranker,
        known_as_of=known_as_of,
        namespace=namespace,
        relation_connection=relation_connection,
        relation_config=relation_config,
        tracer=tracer,
        candidate_limit=candidate_limit,
        ranking_now=ranking_now,
        selected_intent=selected_intent,
        reference=reference,
        preference_boost=(
            config.preference_recency_boost if preference_recency_boost is None else preference_recency_boost
        ),
        query_tags=query_tags,
        tag_boost_enabled=effective_tag_boost_enabled,
        tag_boost_weight=config.tag_boost_weight if tag_boost_weight is None else tag_boost_weight,
        tag_channel_enabled=effective_tag_channel_enabled,
        tag_channel_weight=config.tag_channel_weight if tag_channel_weight is None else tag_channel_weight,
        tag_candidate_limit=effective_tag_candidate_limit,
        fts=fts,
        dense=dense,
        tags=tag_results,
        fts_us=fts_us,
        dense_us=dense_us,
        tag_us=tag_us,
        total_started=total_started,
    )


def _filter_and_score(ctx: RecallContext) -> RecallContext:
    """应用可见性、去重、RRF、反馈率和多因子先验评分。"""
    started = time.perf_counter_ns()
    tracer = ctx.tracer
    visible: list[dict[str, Any]] = []
    for claim in ctx.fts + ctx.dense + ctx.tags:
        if claim_is_visible(claim, ctx.reference, ctx.known_as_of, ctx.selected_intent):
            visible.append(claim)
        elif tracer is not None:
            tracer.record_filter(
                str(claim["id"]),
                _visibility_filter_reason(claim, ctx.reference, ctx.known_as_of, ctx.selected_intent),
            )
    by_id = {claim["id"]: claim for claim in visible}
    for claim_id, helpful_rate in ctx.repo.helpful_rates(list(by_id)).items():
        by_id[claim_id]["helpful_rate"] = helpful_rate
    channels = [(ctx.fts, 1.0), (ctx.dense, 1.0)]
    if ctx.tag_channel_enabled and ctx.tags:
        channels.append((ctx.tags, ctx.tag_channel_weight))
    scores = _weighted_rrf_scores(channels, RRF_K)
    normalization = (2.0 + (ctx.tag_channel_weight if ctx.tags else 0.0)) / (RRF_K + 1)
    max_access = max((_access_count(claim) for claim in by_id.values()), default=0)
    feature_by_id = {
        claim_id: memory_features(claim, scores[claim_id] / normalization, max_access, ctx.ranking_now)
        for claim_id, claim in by_id.items()
    }
    tag_boosts: dict[str, float] = {}
    if ctx.tag_boost_enabled and ctx.query_tags:
        query_tag_set = set(ctx.query_tags)
        for claim_id, claim in by_id.items():
            overlap = query_tag_set.intersection(claim.get("topic_tags") or [])
            weighted = sum(
                TAG_INFO_WEIGHT.get(tag, 0.5)
                for tag in overlap
                if tag not in LOW_INFORMATION_TAGS
            )
            if weighted <= 0.0:
                continue
            boost = min(weighted / len(query_tag_set), 1.0) * ctx.tag_boost_weight
            tag_boosts[claim_id] = boost
            feature_by_id[claim_id]["tag_boost"] = boost
            claim["_tag_boost"] = boost
    pre_scores = {
        claim_id: memory_score(features)
        + tag_boosts.get(claim_id, 0.0)
        + (
            ctx.preference_boost * features["recency"]
            if ctx.selected_intent is RecallIntent.PREFERENCE and _is_preference_claim(by_id[claim_id])
            else 0.0
        )
        for claim_id, features in feature_by_id.items()
    }
    ctx.by_id = by_id
    ctx.feature_by_id = feature_by_id
    ctx.pre_scores = pre_scores
    ctx.tag_boosts = tag_boosts
    ctx.ranked_claims = _sort_pre_rank(by_id, feature_by_id, pre_scores)
    if tracer is not None:
        tracer.trace.tag_boost_applied = bool(tag_boosts)
        tracer.record_tag_boosts(tag_boosts)
        tracer.trace.phases.fusion_us = (time.perf_counter_ns() - started) // 1000
        tracer.record_pre_rank(ctx.ranked_claims, pre_scores)
    return ctx


def _sort_pre_rank(
    by_id: dict[str, dict[str, Any]],
    feature_by_id: dict[str, dict[str, float]],
    pre_scores: dict[str, float],
) -> list[dict[str, Any]]:
    return sorted(
        by_id.values(),
        key=lambda claim: (
            -pre_scores[claim["id"]],
            -feature_by_id[claim["id"]]["semantic"],
            -_recorded_epoch(claim),
            str(claim["id"]),
        ),
    )


def _expand_related(ctx: RecallContext) -> RecallContext:
    """执行可选关系扩展，默认关闭时保持候选不变。"""
    config = ctx.relation_config
    if ctx.relation_connection is None or config is None or not config.enabled:
        return ctx
    started = time.perf_counter_ns()
    seeds = [
        {**claim, "_semantic_score": ctx.feature_by_id[claim["id"]]["semantic"]}
        for claim in ctx.ranked_claims
    ]
    expanded, metadata_items = expand_related_claims(
        ctx.relation_connection,
        ctx.repo,
        seeds,
        ctx.reference,
        ctx.known_as_of,
        ctx.selected_intent,
        ctx.namespace,
        config,
    )
    expanded_ids = [str(claim["id"]) for claim in expanded if str(claim["id"]) not in ctx.by_id]
    expanded_by_id = {str(claim["id"]): claim for claim in expanded if str(claim["id"]) in expanded_ids}
    helpful_rates = ctx.repo.helpful_rates(expanded_ids)
    for claim_id, claim in expanded_by_id.items():
        claim["helpful_rate"] = helpful_rates.get(claim_id, claim.get("helpful_rate", 0.5))
        ctx.by_id[claim_id] = claim
    max_access = max((_access_count(claim) for claim in ctx.by_id.values()), default=0)
    for claim_id, claim in expanded_by_id.items():
        ctx.feature_by_id[claim_id] = memory_features(claim, claim["_semantic_score"], max_access, ctx.ranking_now)
        ctx.pre_scores[claim_id] = memory_score(ctx.feature_by_id[claim_id])
    if expanded_by_id:
        ctx.ranked_claims = _sort_pre_rank(ctx.by_id, ctx.feature_by_id, ctx.pre_scores)
        tracer = ctx.tracer
        if tracer is not None:
            tracer.record_channel("relation", list(expanded_by_id.values()))
            metadata_by_id = {item.claim_id: item for item in metadata_items}
            for claim_id in expanded_by_id:
                metadata = metadata_by_id[claim_id]
                tracer.record_relation_path(
                    claim_id,
                    {
                        "seed_id": metadata.seed_id,
                        "path": [
                            {
                                "from_id": hop.from_id,
                                "to_id": hop.to_id,
                                "relation": hop.relation,
                                "source": hop.source,
                                "edge_confidence": hop.edge_confidence,
                            }
                            for hop in metadata.path
                        ],
                        "cumulative_weight": metadata.cumulative_weight,
                        "expansion_score": metadata.expansion_score,
                    },
                )
    if ctx.tracer is not None:
        ctx.tracer.trace.phases.relation_us = (time.perf_counter_ns() - started) // 1000
    return ctx


def _rerank(ctx: RecallContext) -> RecallContext:
    """调用 reranker，并在空结果或错误时降级到先验排序。"""
    ranked_claims = ctx.ranked_claims
    reranker = ctx.reranker
    ctx.rerank_us = 0
    ctx.reranked = []
    ctx.valid_reranked = []
    ctx.rerank_scores = {}
    ctx.ranked_result = ranked_claims
    if reranker is None:
        ctx.outcome = "disabled"
        return ctx
    if len(ranked_claims) <= 1:
        ctx.outcome = "skipped"
        return ctx

    candidates = ranked_claims[: ctx.candidate_limit]
    started = time.perf_counter_ns()
    try:
        returned = reranker.rerank(
            ctx.query,
            [_claim_text(claim) for claim in candidates],
            top_n=ctx.candidate_limit,
        )
    except Exception:
        ctx.rerank_us = (time.perf_counter_ns() - started) // 1000
        ctx.outcome = "error_fallback"
        return ctx
    ctx.rerank_us = (time.perf_counter_ns() - started) // 1000
    if ctx.tracer is not None:
        ctx.tracer.trace.phases.reranker_us = ctx.rerank_us
    if isinstance(returned, RerankResult):
        reranked, result_status = returned.results, returned.outcome
    else:
        reranked = returned
        last = getattr(reranker, "last_outcome", None)
        result_status = getattr(last, "outcome", None) or last or ("empty" if not reranked else "success")
    ctx.reranked = reranked
    if not reranked:
        ctx.outcome = "error_fallback" if result_status == "error" else "empty_fallback"
        return ctx

    valid = [(candidates[index], score) for index, score in reranked if 0 <= index < len(candidates)]
    ctx.valid_reranked = valid
    raw_scores = {claim["id"]: float(score) for claim, score in valid}
    if ctx.tracer is not None:
        ctx.tracer.record_rerank([(str(claim["id"]), float(score)) for claim, score in valid])
    rerank_scores = {
        claim["id"]: blend_reranker_score(score, ctx.feature_by_id[claim["id"]]) for claim, score in valid
    }
    ctx.rerank_scores = rerank_scores
    reranked_claims = sorted(
        (claim for claim, _ in valid),
        key=lambda claim: (
            -rerank_scores[claim["id"]],
            -raw_scores[claim["id"]],
            -ctx.feature_by_id[claim["id"]]["semantic"],
            -_recorded_epoch(claim),
            str(claim["id"]),
        ),
    )
    if ctx.selected_intent is RecallIntent.PREFERENCE:
        reranked_ids = {claim["id"] for claim in reranked_claims}
        reranked_claims.extend(
            claim for claim in ranked_claims if _is_preference_claim(claim) and claim["id"] not in reranked_ids
        )
    ctx.ranked_result = reranked_claims
    ctx.outcome = "applied"
    return ctx


def _finalize(ctx: RecallContext) -> list[dict[str, Any]]:
    """执行截断、偏好保留、trace、审计和最终分数装配。"""
    final = _preference_first(ctx.ranked_result, ctx.limit, ctx.selected_intent)
    tracer = ctx.tracer
    if tracer is not None:
        final_ids = {str(claim["id"]) for claim in final}
        if ctx.reranked:
            reranked_ids = {str(claim["id"]) for claim, _ in ctx.valid_reranked}
            for claim in ctx.ranked_claims:
                if str(claim["id"]) not in reranked_ids:
                    tracer.record_filter(str(claim["id"]), "reranker_omitted")
        for claim in ctx.ranked_claims:
            claim_id = str(claim["id"])
            if claim_id not in final_ids and claim_id in tracer.trace.candidates:
                tracer.record_filter(claim_id, "final_limit")
        tracer.record_final(final)
        tracer.trace.outcome = ctx.outcome
        tracer.trace.phases.total_us = (time.perf_counter_ns() - ctx.total_started) // 1000
    current_audit().emit(
        "recall",
        "ranked",
        ctx.outcome,
        duration_us=(time.perf_counter_ns() - ctx.total_started) // 1000,
        detail={
            "query_hash": hashlib.sha256(ctx.query.encode()).hexdigest(),
            "limit": ctx.limit,
            "as_of": ctx.as_of,
            "intent": ctx.selected_intent.value,
            "known_as_of": ctx.known_as_of,
            "candidate_limit": ctx.candidate_limit,
            "fts_ids": [item["id"] for item in ctx.fts],
            "dense_ids": [item["id"] for item in ctx.dense],
            "tag_ids": [item["id"] for item in ctx.tags],
            "query_tags": ctx.query_tags,
            "tag_boost_applied": bool(ctx.tag_boosts),
            "tag_boost": ctx.tag_boosts,
            "tag_channel_applied": bool(ctx.tag_channel_enabled and ctx.query_tags and ctx.tags),
            "rrf_ids": [item["id"] for item in ctx.ranked_claims],
            "returned_ids": [item["id"] for item in final],
            "weights": DEFAULT_WEIGHTS,
            "scores": {
                item["id"]: {
                    **ctx.feature_by_id[item["id"]],
                    "pre_rank": ctx.pre_scores[item["id"]],
                    "final": (
                        ctx.rerank_scores.get(item["id"], ctx.pre_scores[item["id"]])
                        if ctx.reranked
                        else ctx.pre_scores[item["id"]]
                    ),
                }
                for item in final
            },
            "timing_us": {
                "fts": ctx.fts_us,
                "dense": ctx.dense_us,
                "tag": ctx.tag_us,
                "reranker": ctx.rerank_us,
            },
        },
    )
    for claim in final:
        claim["_score"] = ctx.rerank_scores.get(claim["id"], ctx.pre_scores[claim["id"]])
    return final
