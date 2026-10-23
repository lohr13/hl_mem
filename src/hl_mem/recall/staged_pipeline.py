"""分阶段的混合召回、排序、关系扩展与收尾实现。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hl_mem.core.vector import normalized_cosine_similarity, normalized_vector
from hl_mem.domain.claims.attributes import SLOT_REGISTRY, normalize_predicate
from hl_mem.domain.claims.dedup import (
    DETERMINISTIC_NEAR_COPY_REASON,
    is_safe_near_duplicate,
)
from hl_mem.domain.claims.query_tags import (
    LOW_INFORMATION_TAGS,
    TAG_INFO_WEIGHT,
    extract_query_slot_hints,
    extract_query_tags,
)
from hl_mem.domain.recall import RecallIntent, route_recall_intent
from hl_mem.domain.temporal import claim_is_visible
from hl_mem.observability.audit import current_audit
from hl_mem.protocols import RerankerProtocol, WeightedQuery
from hl_mem.recall.candidate_channels import ChannelRequest, collect_query_channels
from hl_mem.recall.echo_suppression import (
    DEFAULT_ECHO_SUPPRESSION_METRICS,
    EchoRequest,
    EchoSuppressionPolicy,
)
from hl_mem.recall.ranking import (
    DEFAULT_WEIGHTS,
    blend_reranker_score,
    decay_ranking_weights,
    memory_features,
    memory_score,
)
from hl_mem.recall.relation_expansion import (
    RelationExpansionConfig,
    expand_related_claims,
)
from hl_mem.recall.reranker import RerankResult
from hl_mem.recall.trace import SearchTracer
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository

# ── 排序因子冻结 ──────────────────────────────────────────────
# 排序链已稳定，不再增加新 boost/channel/weight。
# 新增召回能力应通过已有通道（FTS/Dense/Tag）的参数调优实现，
# 而非引入新的排序因子。如需新增，必须先建立离线评测集并证明不退化。
# ──────────────────────────────────────────────────────────────

RRF_K = 60


class EntityScopeFallback(RuntimeError):
    """A scoped read failed and the application must retry the original query wide."""

    def __init__(self, reason: str, original_error: sqlite3.Error) -> None:
        super().__init__(reason)
        self.reason = reason
        self.original_error = original_error


@dataclass(frozen=True)
class RecallConfig:
    """召回管线使用的完整排序配置。"""

    vector_scan_limit: int = field(default_factory=lambda: Settings().recall_vector_scan_limit)
    dense_enabled: bool = True
    candidate_floor: int = 50
    tag_boost_enabled: bool = True
    tag_boost_weight: float = 0.05
    preference_recency_boost: float = 0.12
    dedup_threshold: float = 0.0
    dedup_candidate_limit: int = 100
    feedback_min_samples: int = field(default_factory=lambda: Settings().feedback_min_samples)
    decay_model: str = "legacy_linear"
    entity_scope_mode: str = "off"
    entity_scope_id: str | None = None

    @property
    def entity_constraint_mode(self) -> str:
        return self.entity_scope_mode

    @property
    def entity_filter_id(self) -> str | None:
        return self.entity_scope_id


@dataclass
class RecallContext:
    """召回管线各阶段的共享上下文。"""

    repo: ClaimRepository
    query: str = ""
    query_blob: bytes = b""
    limit: int = 5
    as_of: str | None = None
    reranker: RerankerProtocol | None = None
    known_as_of: str | None = None
    namespace: str = "default"
    relation_connection: sqlite3.Connection | None = None
    relation_config: RelationExpansionConfig | None = None
    tracer: SearchTracer | None = None

    candidate_limit: int = 50
    ranking_now: str = ""
    selected_intent: RecallIntent = RecallIntent.CURRENT_STATE
    reference: str = ""
    preference_boost: float = 0.12
    query_tags: list[str] = field(default_factory=list)
    query_slot_hints: list[str] = field(default_factory=list)
    tag_boost_enabled: bool = True
    tag_boost_weight: float = 0.05
    dedup_threshold: float = 0.0
    dedup_candidate_limit: int = 100
    feedback_min_samples: int = 3
    ranking_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    fts: list[dict[str, Any]] = field(default_factory=list)
    dense: list[dict[str, Any]] = field(default_factory=list)
    query_channels: list[tuple[str, list[dict[str, Any]], float, float]] = field(default_factory=list)
    fts_us: int = 0
    dense_us: int = 0
    total_started: int = 0

    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    feature_by_id: dict[str, dict[str, float]] = field(default_factory=dict)
    pre_scores: dict[str, float] = field(default_factory=dict)
    tag_boosts: dict[str, float] = field(default_factory=dict)
    ranked_claims: list[dict[str, Any]] = field(default_factory=list)

    rerank_us: int = 0
    reranked: list[tuple[int, float]] = field(default_factory=list)
    valid_reranked: list[tuple[dict[str, Any], float]] = field(default_factory=list)
    rerank_scores: dict[str, float] = field(default_factory=dict)
    ranked_result: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = ""
    echo_policy: EchoSuppressionPolicy | None = None
    echo_request: EchoRequest | None = None
    echo_signal_loader: Callable[[list[str]], dict[str, dict[str, object]]] | None = None


def _claim_text(claim: dict[str, Any]) -> str:
    index_text = claim.get("index_text")
    return index_text if isinstance(index_text, str) else ""


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
    """Truncate to limit; preference intent is handled by score boost in _filter_and_score."""
    return claims[:limit]


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
    channels: Sequence[tuple[list[dict[str, Any]], float] | tuple[list[dict[str, Any]], float, float]],
    rank_constant: int,
) -> dict[str, float]:
    """按查询权重和通道权重计算 RRF，空通道不产生分数。"""
    scores: dict[str, float] = {}
    for entry in channels:
        channel, query_weight, channel_weight = (*entry, 1.0) if len(entry) == 2 else entry
        for rank, item in enumerate(channel, 1):
            item_id = str(item["id"])
            scores[item_id] = scores.get(item_id, 0.0) + query_weight * channel_weight / (rank_constant + rank)
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
    reranker: RerankerProtocol | None = None,
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
    weighted_queries: list[WeightedQuery] | None = None,
    query_blobs: list[bytes] | None = None,
    low_recall_expander: Callable[[int, int], tuple[list[WeightedQuery], list[bytes]]] | None = None,
    echo_policy: EchoSuppressionPolicy | None = None,
    echo_request: EchoRequest | None = None,
    echo_signal_loader: Callable[[list[str]], dict[str, dict[str, object]]] | None = None,
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
        weighted_queries=weighted_queries,
        query_blobs=query_blobs,
        low_recall_expander=low_recall_expander,
        echo_policy=echo_policy,
        echo_request=echo_request,
        echo_signal_loader=echo_signal_loader,
    )
    return _finalize(_rerank(_apply_echo_suppression(_expand_related(_filter_and_score(state)))))


def _collect_candidates(
    repo: ClaimRepository,
    query: str,
    query_blob: bytes,
    limit: int,
    as_of: str | None,
    reranker: RerankerProtocol | None = None,
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
    weighted_queries: list[WeightedQuery] | None = None,
    query_blobs: list[bytes] | None = None,
    low_recall_expander: Callable[[int, int], tuple[list[WeightedQuery], list[bytes]]] | None = None,
    echo_policy: EchoSuppressionPolicy | None = None,
    echo_request: EchoRequest | None = None,
    echo_signal_loader: Callable[[list[str]], dict[str, dict[str, object]]] | None = None,
) -> RecallContext:
    """仅执行 FTS 与向量检索，并建立统一时间快照。"""
    config = recall_config or RecallConfig()
    effective_floor = candidate_floor or config.candidate_floor
    candidate_limit = min(config.vector_scan_limit, max(limit * 5, effective_floor))
    ranking_now = now or datetime.now(timezone.utc).isoformat()
    selected_intent = RecallIntent(intent) if intent else route_recall_intent(query, as_of, ranking_now)
    reference = as_of or ranking_now
    total_started = time.perf_counter_ns()
    effective_tag_boost_enabled = config.tag_boost_enabled if tag_boost_enabled is None else tag_boost_enabled
    query_tags = extract_query_tags(query) if effective_tag_boost_enabled else []
    query_slot_hints, hinted_tags = extract_query_slot_hints(query)
    query_tags = list(dict.fromkeys([*query_tags, *hinted_tags]))

    queries = weighted_queries or [WeightedQuery(query, "original", 1.0)]
    blobs = query_blobs or [query_blob]
    if len(queries) != len(blobs):
        raise ValueError("weighted_queries and query_blobs must have equal lengths")
    query_channels: list[tuple[str, list[dict[str, Any]], float, float]] = []
    fts_us = 0
    dense_us = 0
    entity_filtered_ids: set[str] = set()
    channel_request = ChannelRequest(
        candidate_limit=candidate_limit,
        reference=reference,
        selected_intent=selected_intent,
        known_as_of=known_as_of,
        namespace=namespace,
        dense_enabled=config.dense_enabled,
        entity_scope_mode=config.entity_scope_mode,
        entity_scope_id=config.entity_scope_id,
    )

    def collect_query(item: WeightedQuery, blob: bytes, index: int) -> None:
        nonlocal fts_us, dense_us
        scope_started = time.perf_counter_ns()
        try:
            collected = collect_query_channels(repo, item, blob, index, channel_request)
        except sqlite3.Error as error:
            if config.entity_scope_mode != "entity" or config.entity_scope_id is None:
                raise
            if tracer is not None:
                tracer.trace.entity_scope_us += (time.perf_counter_ns() - scope_started) // 1000
                tracer.trace.entity_fallback_reason = "storage_error"
                tracer.trace.entity_filter_mode = "wide"
            raise EntityScopeFallback("storage_error", error) from error
        if tracer is not None:
            tracer.trace.entity_scope_us += collected.entity_scope_us
            if collected.entity_scope_applied:
                for channel, count in collected.entity_scope_counts.items():
                    tracer.trace.entity_scope_counts[channel] = tracer.trace.entity_scope_counts.get(channel, 0) + count
        query_channels.extend(collected.channels)
        fts_us += collected.fts_us
        dense_us += collected.dense_us
        entity_filtered_ids.update(collected.filtered_ids)
        if tracer is not None:
            legacy = len(queries) == 1
            for name, results, _, _ in collected.channels:
                tracer.record_channel(name.split(":")[-1] if legacy and index == 0 else name, results)

    collect_query(queries[0], blobs[0], 0)
    original_visible_count = len(
        {
            str(claim["id"])
            for _, channel, _, _ in query_channels
            for claim in channel
            if claim_is_visible(claim, reference, known_as_of, selected_intent)
        }
    )
    original_fts_visible_count = len(
        {
            str(claim["id"])
            for claim in query_channels[0][1]
            if claim_is_visible(claim, reference, known_as_of, selected_intent)
        }
    )
    if len(queries) == 1 and low_recall_expander is not None:
        extra_queries, extra_blobs = low_recall_expander(original_visible_count, original_fts_visible_count)
        queries.extend(extra_queries)
        blobs.extend(extra_blobs)
    for index, (item, blob) in enumerate(zip(queries[1:], blobs[1:]), 1):
        collect_query(item, blob, index)
    fts = query_channels[0][1]
    dense = next((channel for name, channel, _, _ in query_channels if name == "original:dense"), [])
    if tracer is not None:
        tracer.trace.candidate_limit = candidate_limit
        tracer.trace.phases.fts_us = fts_us
        tracer.trace.phases.dense_us = dense_us

    if tracer is not None:
        tracer.trace.query_tags = query_tags
        tracer.trace.query_slot_hints = query_slot_hints
        if config.entity_scope_id is not None:
            tracer.trace.entity_filter_mode = (
                "enforce" if config.entity_scope_mode == "entity" else config.entity_scope_mode
            )
            tracer.trace.entity_filtered_count = len(entity_filtered_ids)
        tracer.trace.tag_boost_applied = bool(effective_tag_boost_enabled and query_tags)

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
        query_slot_hints=query_slot_hints,
        tag_boost_enabled=effective_tag_boost_enabled,
        tag_boost_weight=config.tag_boost_weight if tag_boost_weight is None else tag_boost_weight,
        dedup_threshold=config.dedup_threshold,
        dedup_candidate_limit=config.dedup_candidate_limit,
        feedback_min_samples=config.feedback_min_samples,
        ranking_weights=decay_ranking_weights(config.decay_model),
        fts=fts,
        dense=dense,
        query_channels=query_channels,
        fts_us=fts_us,
        dense_us=dense_us,
        total_started=total_started,
        echo_policy=echo_policy,
        echo_request=echo_request,
        echo_signal_loader=echo_signal_loader,
    )


def _filter_and_score(ctx: RecallContext) -> RecallContext:
    """应用可见性、去重、RRF、反馈率和多因子先验评分。"""
    started = time.perf_counter_ns()
    tracer = ctx.tracer
    visible: list[dict[str, Any]] = []
    semantic_candidates = [claim for _, channel, _, _ in ctx.query_channels for claim in channel]
    for claim in semantic_candidates:
        if claim_is_visible(claim, ctx.reference, ctx.known_as_of, ctx.selected_intent):
            visible.append(claim)
        elif tracer is not None:
            tracer.record_filter(
                str(claim["id"]),
                _visibility_filter_reason(claim, ctx.reference, ctx.known_as_of, ctx.selected_intent),
            )
    by_id = {claim["id"]: claim for claim in visible}
    for claim_id, helpful_rate in ctx.repo.helpful_rates(
        list(by_id),
        ctx.feedback_min_samples,
    ).items():
        by_id[claim_id]["helpful_rate"] = helpful_rate
    channels = [(items, query_weight, channel_weight) for _, items, query_weight, channel_weight in ctx.query_channels]
    scores = _weighted_rrf_scores(channels, RRF_K)
    enabled_weight = sum(query_weight * channel_weight for _, _, query_weight, channel_weight in ctx.query_channels)
    normalization = enabled_weight / (RRF_K + 1)
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
            weighted = sum(TAG_INFO_WEIGHT.get(tag, 0.5) for tag in overlap if tag not in LOW_INFORMATION_TAGS)
            if weighted <= 0.0:
                continue
            boost = min(weighted / len(query_tag_set), 1.0) * ctx.tag_boost_weight
            tag_boosts[claim_id] = boost
            feature_by_id[claim_id]["tag_boost"] = boost
            claim["_tag_boost"] = boost
    pre_scores = {
        claim_id: memory_score(features, ctx.ranking_weights)
        + tag_boosts.get(claim_id, 0.0)
        + (0.05 if any(_claim_matches_slot_hint(by_id[claim_id], hint) for hint in ctx.query_slot_hints) else 0.0)
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
        tracer.trace.slot_boost_applied = any(
            any(_claim_matches_slot_hint(claim, hint) for hint in ctx.query_slot_hints) for claim in by_id.values()
        )
        tracer.trace.tag_boost_applied = bool(tag_boosts)
        tracer.record_tag_boosts(tag_boosts)
        tracer.trace.phases.fusion_us = (time.perf_counter_ns() - started) // 1000
        tracer.record_pre_rank(ctx.ranked_claims, pre_scores)
    return ctx


def _slot_matches(slot: str, hint: str) -> bool:
    """匹配精确 slot 或 preference 通配 hint。"""
    return slot.startswith("preference.") if hint == "preference.*" else slot == hint


def _claim_matches_slot_hint(claim: dict[str, Any], hint: str) -> bool:
    """让 operational slot hint 同时匹配新 slot 与兼容 canonical attribute。"""
    return any(_slot_matches(str(claim.get(field) or ""), hint) for field in ("canonical_slot", "canonical_attribute"))


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
    seeds = [{**claim, "_semantic_score": ctx.feature_by_id[claim["id"]]["semantic"]} for claim in ctx.ranked_claims]
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
    helpful_rates = ctx.repo.helpful_rates(expanded_ids, ctx.feedback_min_samples)
    for claim_id, claim in expanded_by_id.items():
        claim["helpful_rate"] = helpful_rates.get(claim_id, claim.get("helpful_rate", 0.5))
        ctx.by_id[claim_id] = claim
    max_access = max((_access_count(claim) for claim in ctx.by_id.values()), default=0)
    for claim_id, claim in expanded_by_id.items():
        ctx.feature_by_id[claim_id] = memory_features(claim, claim["_semantic_score"], max_access, ctx.ranking_now)
        ctx.pre_scores[claim_id] = memory_score(ctx.feature_by_id[claim_id], ctx.ranking_weights)
    if expanded_by_id:
        ctx.ranked_claims = _sort_pre_rank(ctx.by_id, ctx.feature_by_id, ctx.pre_scores)
    tracer = ctx.tracer
    if tracer is not None and expanded:
        tracer.record_channel("relation", expanded)
        for metadata in metadata_items:
            tracer.record_relation_path(
                metadata.claim_id,
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
    except Exception as error:
        ctx.rerank_us = (time.perf_counter_ns() - started) // 1000
        ctx.outcome = "error_fallback"
        if ctx.tracer is not None:
            ctx.tracer.trace.phases.reranker_us = ctx.rerank_us
            ctx.tracer.trace.reranker_error_class = type(error).__name__
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
        if ctx.tracer is not None and ctx.outcome == "error_fallback":
            ctx.tracer.trace.reranker_error_class = getattr(reranker, "last_error_class", None) or "RerankerError"
        return ctx

    valid = [(candidates[index], score) for index, score in reranked if 0 <= index < len(candidates)]
    ctx.valid_reranked = valid
    raw_scores = {claim["id"]: float(score) for claim, score in valid}
    if ctx.tracer is not None:
        ctx.tracer.record_rerank([(str(claim["id"]), float(score)) for claim, score in valid])
    rerank_scores = {
        claim["id"]: blend_reranker_score(
            score,
            ctx.feature_by_id[claim["id"]],
            ctx.ranking_weights,
        )
        for claim, score in valid
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
    ctx.ranked_result = reranked_claims
    ctx.outcome = "applied"
    return ctx


def _apply_echo_suppression(ctx: RecallContext) -> RecallContext:
    """Fail-open provenance policy between relation expansion and reranking."""
    policy = ctx.echo_policy
    request = ctx.echo_request
    if policy is None or request is None:
        return ctx
    claim_ids = [str(claim["id"]) for claim in ctx.ranked_claims]
    if policy.mode == "off":
        evaluation = policy.evaluate(claim_ids, request, {})
    elif ctx.echo_signal_loader is None:
        evaluation = policy.read_failure(claim_ids, "signal_loader_missing")
    else:
        try:
            signals = ctx.echo_signal_loader(claim_ids)
        except Exception:
            evaluation = policy.read_failure(claim_ids)
        else:
            evaluation = policy.evaluate(claim_ids, request, signals)
    DEFAULT_ECHO_SUPPRESSION_METRICS.record(evaluation)
    if ctx.tracer is not None:
        ctx.tracer.trace.injection["echo_suppression"] = evaluation.summary()
    suppressed_ids: set[str] = set()
    for decision in evaluation.decisions:
        for reason in decision.trace_reasons:
            if ctx.tracer is not None:
                ctx.tracer.record_filter(decision.claim_id, reason)
        if decision.suppress:
            suppressed_ids.add(decision.claim_id)
        if decision.matched_reason is not None:
            current_audit().emit(
                "recall",
                "echo_suppression",
                "suppressed" if decision.suppress else "would_suppress",
                query_id=ctx.tracer.trace.query_id if ctx.tracer is not None else None,
                claim_id=decision.claim_id,
                detail={
                    "reason": decision.matched_reason,
                    "age_bucket": decision.age_bucket,
                    "similarity_bucket": decision.similarity_bucket,
                    "policy_version": request.policy_version,
                    "experiment_variant": request.experiment_variant,
                },
            )
    if suppressed_ids:
        ctx.ranked_claims = [claim for claim in ctx.ranked_claims if str(claim["id"]) not in suppressed_ids]
    return ctx


def _finalize(ctx: RecallContext) -> list[dict[str, Any]]:
    """执行截断、偏好保留、trace、审计和最终分数装配。"""
    for claim in ctx.ranked_result:
        claim["_score"] = ctx.rerank_scores.get(claim["id"], ctx.pre_scores[claim["id"]])
        claim["_score_path"] = "reranker_applied" if claim["id"] in ctx.rerank_scores else "reranker_fallback"
        claim["_reranker_raw_score"] = next(
            (float(score) for candidate, score in ctx.valid_reranked if candidate["id"] == claim["id"]),
            None,
        )
        claim["_pre_score"] = ctx.pre_scores[claim["id"]]
        claim["_features"] = dict(ctx.feature_by_id[claim["id"]])
    equivalent_pairs = _confirmed_equivalent_pairs(
        ctx.relation_connection,
        [str(claim["id"]) for claim in ctx.ranked_result[: ctx.dedup_candidate_limit]],
        ctx.dedup_threshold,
    )
    folded = fold_similar_claims(
        ctx.ranked_result,
        ctx.dedup_threshold,
        ctx.dedup_candidate_limit,
        equivalent_pairs=equivalent_pairs,
    )
    final = _preference_first(folded, ctx.limit, ctx.selected_intent)
    tracer = ctx.tracer
    if tracer is not None:
        final_ids = {str(claim["id"]) for claim in final}
        folded_alias_ids = {str(alias_id) for claim in folded for alias_id in claim.get("_equivalent_claim_ids") or []}
        if ctx.reranked:
            reranked_ids = {str(claim["id"]) for claim, _ in ctx.valid_reranked}
            for claim in ctx.ranked_claims:
                if str(claim["id"]) not in reranked_ids:
                    tracer.record_filter(str(claim["id"]), "reranker_omitted")
        for claim in ctx.ranked_claims:
            claim_id = str(claim["id"])
            if claim_id not in final_ids and claim_id in tracer.trace.candidates:
                tracer.record_filter(
                    claim_id,
                    "equivalent_folded" if claim_id in folded_alias_ids else "final_limit",
                )
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
            "query_tags": ctx.query_tags,
            "tag_boost_applied": bool(ctx.tag_boosts),
            "tag_boost": ctx.tag_boosts,
            "rrf_ids": [item["id"] for item in ctx.ranked_claims],
            "returned_ids": [item["id"] for item in final],
            "weights": ctx.ranking_weights,
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
                "reranker": ctx.rerank_us,
            },
        },
    )
    return final


def _confirmed_equivalent_pairs(
    connection: sqlite3.Connection | None,
    claim_ids: list[str],
    threshold: float,
) -> list[tuple[str, str, float]]:
    """Load a bounded set of deterministic equivalent edges for current candidates."""
    unique_ids = list(dict.fromkeys(claim_ids))
    if connection is None or threshold <= 0.0 or len(unique_ids) < 2:
        return []
    placeholders = ",".join("?" for _ in unique_ids)
    rows = connection.execute(
        "SELECT left_claim_id,right_claim_id,similarity FROM dedup_pairs "
        "WHERE decision='equivalent' AND judge_reason=? AND similarity>=? "
        f"AND left_claim_id IN ({placeholders}) AND right_claim_id IN ({placeholders}) "
        "ORDER BY similarity DESC,reviewed_at,id LIMIT ?",
        (
            DETERMINISTIC_NEAR_COPY_REASON,
            threshold,
            *unique_ids,
            *unique_ids,
            max(len(unique_ids), len(unique_ids) * 4),
        ),
    ).fetchall()
    return [(str(row["left_claim_id"]), str(row["right_claim_id"]), float(row["similarity"])) for row in rows]


def _append_folded_claim(representative: dict[str, Any], folded: dict[str, Any]) -> None:
    aliases = representative.setdefault("_equivalent_claim_ids", [])
    for claim_id in [folded.get("id"), *(folded.get("_equivalent_claim_ids") or [])]:
        normalized = str(claim_id or "")
        if normalized and normalized != str(representative.get("id")) and normalized not in aliases:
            aliases.append(normalized)


def fold_similar_claims(
    claims: list[dict[str, Any]],
    threshold: float,
    candidate_limit: int = 100,
    *,
    equivalent_pairs: Sequence[tuple[str, str, float]] | None = None,
) -> list[dict[str, Any]]:
    """在兼容语义桶内折叠高相似 Claim，并限制同步比较窗口。"""
    if threshold <= 0.0:
        return list(claims)
    if candidate_limit < 1:
        raise ValueError("candidate_limit must be positive")
    ranked = sorted(claims, key=lambda item: -float(item.get("_score", 0.0)))
    fold_candidates = ranked[:candidate_limit]
    untouched = ranked[candidate_limit:]
    candidates_by_id = {str(claim["id"]): claim for claim in fold_candidates}
    parents = {claim_id: claim_id for claim_id in candidates_by_id}

    def find(claim_id: str) -> str:
        while parents[claim_id] != claim_id:
            parents[claim_id] = parents[parents[claim_id]]
            claim_id = parents[claim_id]
        return claim_id

    def union(left_id: str, right_id: str) -> None:
        left_root, right_root = find(left_id), find(right_id)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_id, right_id, similarity in equivalent_pairs or ():
        left = candidates_by_id.get(left_id)
        right = candidates_by_id.get(right_id)
        if left is None or right is None:
            continue
        if is_safe_near_duplicate(
            left,
            right,
            similarity=similarity,
            semantic_threshold=threshold,
            allow_subject_mismatch=True,
        ):
            union(left_id, right_id)

    decoded = {
        str(claim["id"]): normalized_vector(embedding)
        for claim in fold_candidates
        if (embedding := claim.get("embedding_dense")) is not None
    }
    kept: list[dict[str, Any]] = []
    kept_near_copy_candidates: list[tuple[dict[str, Any], tuple[float, ...]]] = []
    equivalent_representatives: dict[str, dict[str, Any]] = {}
    for claim in fold_candidates:
        claim_id = str(claim["id"])
        group_root = find(claim_id)
        representative = equivalent_representatives.get(group_root)
        if representative is not None:
            _append_folded_claim(representative, claim)
            continue
        equivalent_representatives[group_root] = claim
        vector = decoded.get(claim_id)
        near_copy_representative = None
        if vector is not None:
            near_copy_representative = next(
                (
                    retained_claim
                    for retained_claim, retained_vector in kept_near_copy_candidates
                    if is_safe_near_duplicate(
                        claim,
                        retained_claim,
                        similarity=normalized_cosine_similarity(vector, retained_vector),
                        semantic_threshold=threshold,
                        allow_subject_mismatch=True,
                    )
                ),
                None,
            )
        if near_copy_representative is not None:
            _append_folded_claim(near_copy_representative, claim)
            equivalent_representatives[group_root] = near_copy_representative
            continue
        kept.append(claim)
        if vector is not None:
            kept_near_copy_candidates.append((claim, vector))
    kept.extend(untouched)
    original_order = {str(claim["id"]): index for index, claim in enumerate(claims)}
    return sorted(kept, key=lambda item: original_order[str(item["id"])])
