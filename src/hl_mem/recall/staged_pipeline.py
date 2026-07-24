"""分阶段的混合召回、排序、关系扩展与收尾实现。"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from hl_mem.config import RECALL_VECTOR_SCAN_LIMIT
from hl_mem.domain.claims.attributes import SLOT_REGISTRY
from hl_mem.domain.recall import RecallIntent, claim_is_visible, route_recall_intent
from hl_mem.observability.audit import current_audit
from hl_mem.recall.ranking import DEFAULT_WEIGHTS, blend_reranker_score, memory_features, memory_score
from hl_mem.recall.relation_expansion import RelationExpansionConfig, expand_related_claims
from hl_mem.recall.reranker import RerankResult
from hl_mem.recall.trace import SearchTracer
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository

RRF_K = 60


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
    definition = SLOT_REGISTRY.get(str(claim.get("canonical_slot") or ""))
    return definition is not None and definition.predicate == "偏好"


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
    reranker: Any = None,
    now: str | None = None,
    intent: RecallIntent | str | None = None,
    known_as_of: str | None = None,
    namespace: str = "default",
    *,
    relation_connection: Any | None = None,
    relation_config: RelationExpansionConfig | None = None,
    tracer: SearchTracer | None = None,
    candidate_floor: int | None = None,
    preference_recency_boost: float | None = None,
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
        relation_connection=relation_connection,
        relation_config=relation_config,
        tracer=tracer,
        candidate_floor=candidate_floor,
        preference_recency_boost=preference_recency_boost,
    )
    return _finalize(_rerank(_expand_related(_filter_and_score(state))))


def _collect_candidates(
    repo: ClaimRepository,
    query: str,
    query_blob: bytes,
    limit: int,
    as_of: str | None,
    reranker: Any = None,
    now: str | None = None,
    intent: RecallIntent | str | None = None,
    known_as_of: str | None = None,
    namespace: str = "default",
    *,
    relation_connection: Any | None = None,
    relation_config: RelationExpansionConfig | None = None,
    tracer: SearchTracer | None = None,
    candidate_floor: int | None = None,
    preference_recency_boost: float | None = None,
) -> dict[str, Any]:
    """仅执行 FTS 与向量检索，并建立统一时间快照。"""
    defaults = Settings()
    effective_floor = candidate_floor or defaults.recall_candidate_floor
    candidate_limit = min(RECALL_VECTOR_SCAN_LIMIT, max(limit * 5, effective_floor))
    ranking_now = now or datetime.now(timezone.utc).isoformat()
    selected_intent = RecallIntent(intent) if intent else route_recall_intent(query, as_of, ranking_now)
    reference = as_of or ranking_now
    total_started = time.perf_counter_ns()

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

    return {
        "repo": repo,
        "query": query,
        "limit": limit,
        "as_of": as_of,
        "reranker": reranker,
        "known_as_of": known_as_of,
        "namespace": namespace,
        "relation_connection": relation_connection,
        "relation_config": relation_config,
        "tracer": tracer,
        "candidate_limit": candidate_limit,
        "ranking_now": ranking_now,
        "selected_intent": selected_intent,
        "reference": reference,
        "preference_boost": (
            defaults.preference_recency_boost if preference_recency_boost is None else preference_recency_boost
        ),
        "fts": fts,
        "dense": dense,
        "fts_us": fts_us,
        "dense_us": dense_us,
        "total_started": total_started,
    }


def _filter_and_score(state: dict[str, Any]) -> dict[str, Any]:
    """应用可见性、去重、RRF、反馈率和多因子先验评分。"""
    started = time.perf_counter_ns()
    tracer = state["tracer"]
    visible: list[dict[str, Any]] = []
    for claim in state["fts"] + state["dense"]:
        if claim_is_visible(claim, state["reference"], state["known_as_of"], state["selected_intent"]):
            visible.append(claim)
        elif tracer is not None:
            tracer.record_filter(
                str(claim["id"]),
                _visibility_filter_reason(claim, state["reference"], state["known_as_of"], state["selected_intent"]),
            )
    by_id = {claim["id"]: claim for claim in visible}
    for claim_id, helpful_rate in state["repo"].helpful_rates(list(by_id)).items():
        by_id[claim_id]["helpful_rate"] = helpful_rate
    scores = _rrf_scores([state["fts"], state["dense"]], RRF_K)
    max_access = max((_access_count(claim) for claim in by_id.values()), default=0)
    feature_by_id = {
        claim_id: memory_features(claim, scores[claim_id] / (2 / (RRF_K + 1)), max_access, state["ranking_now"])
        for claim_id, claim in by_id.items()
    }
    pre_scores = {
        claim_id: memory_score(features)
        + (
            state["preference_boost"] * features["recency"]
            if state["selected_intent"] is RecallIntent.PREFERENCE and _is_preference_claim(by_id[claim_id])
            else 0.0
        )
        for claim_id, features in feature_by_id.items()
    }
    state.update(
        by_id=by_id,
        feature_by_id=feature_by_id,
        pre_scores=pre_scores,
        ranked_claims=_sort_pre_rank(by_id, feature_by_id, pre_scores),
    )
    if tracer is not None:
        tracer.trace.phases.fusion_us = (time.perf_counter_ns() - started) // 1000
        tracer.record_pre_rank(state["ranked_claims"], pre_scores)
    return state


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


def _expand_related(state: dict[str, Any]) -> dict[str, Any]:
    """执行可选关系扩展，默认关闭时保持候选不变。"""
    config = state["relation_config"]
    if state["relation_connection"] is None or config is None or not config.enabled:
        return state
    started = time.perf_counter_ns()
    seeds = [
        {**claim, "_semantic_score": state["feature_by_id"][claim["id"]]["semantic"]}
        for claim in state["ranked_claims"]
    ]
    expanded, metadata_items = expand_related_claims(
        state["relation_connection"],
        state["repo"],
        seeds,
        state["reference"],
        state["known_as_of"],
        state["selected_intent"],
        state["namespace"],
        config,
    )
    expanded_ids = [str(claim["id"]) for claim in expanded if str(claim["id"]) not in state["by_id"]]
    expanded_by_id = {str(claim["id"]): claim for claim in expanded if str(claim["id"]) in expanded_ids}
    helpful_rates = state["repo"].helpful_rates(expanded_ids)
    for claim_id, claim in expanded_by_id.items():
        claim["helpful_rate"] = helpful_rates.get(claim_id, claim.get("helpful_rate", 0.5))
        state["by_id"][claim_id] = claim
    max_access = max((_access_count(claim) for claim in state["by_id"].values()), default=0)
    for claim_id, claim in expanded_by_id.items():
        state["feature_by_id"][claim_id] = memory_features(
            claim, claim["_semantic_score"], max_access, state["ranking_now"]
        )
        state["pre_scores"][claim_id] = memory_score(state["feature_by_id"][claim_id])
    if expanded_by_id:
        state["ranked_claims"] = _sort_pre_rank(state["by_id"], state["feature_by_id"], state["pre_scores"])
        tracer = state["tracer"]
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
    if state["tracer"] is not None:
        state["tracer"].trace.phases.relation_us = (time.perf_counter_ns() - started) // 1000
    return state


def _rerank(state: dict[str, Any]) -> dict[str, Any]:
    """调用 reranker，并在空结果或错误时降级到先验排序。"""
    ranked_claims = state["ranked_claims"]
    reranker = state["reranker"]
    state.update(rerank_us=0, reranked=[], valid_reranked=[], rerank_scores={}, ranked_result=ranked_claims)
    if reranker is None:
        state["outcome"] = "disabled"
        return state
    if len(ranked_claims) <= 1:
        state["outcome"] = "skipped"
        return state

    candidates = ranked_claims[: state["candidate_limit"]]
    started = time.perf_counter_ns()
    returned = reranker.rerank(
        state["query"], [_claim_text(claim) for claim in candidates], top_n=state["candidate_limit"]
    )
    state["rerank_us"] = (time.perf_counter_ns() - started) // 1000
    if state["tracer"] is not None:
        state["tracer"].trace.phases.reranker_us = state["rerank_us"]
    if isinstance(returned, RerankResult):
        reranked, result_status = returned.results, returned.outcome
    else:
        reranked = returned
        last = getattr(reranker, "last_outcome", None)
        result_status = getattr(last, "outcome", None) or last or ("empty" if not reranked else "success")
    state["reranked"] = reranked
    if not reranked:
        state["outcome"] = "error_fallback" if result_status == "error" else "empty_fallback"
        return state

    valid = [(candidates[index], score) for index, score in reranked if 0 <= index < len(candidates)]
    state["valid_reranked"] = valid
    raw_scores = {claim["id"]: float(score) for claim, score in valid}
    if state["tracer"] is not None:
        state["tracer"].record_rerank([(str(claim["id"]), float(score)) for claim, score in valid])
    rerank_scores = {
        claim["id"]: blend_reranker_score(score, state["feature_by_id"][claim["id"]]) for claim, score in valid
    }
    state["rerank_scores"] = rerank_scores
    reranked_claims = sorted(
        (claim for claim, _ in valid),
        key=lambda claim: (
            -rerank_scores[claim["id"]],
            -raw_scores[claim["id"]],
            -state["feature_by_id"][claim["id"]]["semantic"],
            -_recorded_epoch(claim),
            str(claim["id"]),
        ),
    )
    if state["selected_intent"] is RecallIntent.PREFERENCE:
        reranked_ids = {claim["id"] for claim in reranked_claims}
        reranked_claims.extend(
            claim for claim in ranked_claims if _is_preference_claim(claim) and claim["id"] not in reranked_ids
        )
    state["ranked_result"] = reranked_claims
    state["outcome"] = "applied"
    return state


def _finalize(state: dict[str, Any]) -> list[dict[str, Any]]:
    """执行截断、偏好保留、trace、审计和最终分数装配。"""
    final = _preference_first(state["ranked_result"], state["limit"], state["selected_intent"])
    tracer = state["tracer"]
    if tracer is not None:
        final_ids = {str(claim["id"]) for claim in final}
        if state["reranked"]:
            reranked_ids = {str(claim["id"]) for claim, _ in state["valid_reranked"]}
            for claim in state["ranked_claims"]:
                if str(claim["id"]) not in reranked_ids:
                    tracer.record_filter(str(claim["id"]), "reranker_omitted")
        for claim in state["ranked_claims"]:
            claim_id = str(claim["id"])
            if claim_id not in final_ids and claim_id in tracer.trace.candidates:
                tracer.record_filter(claim_id, "final_limit")
        tracer.record_final(final)
        tracer.trace.outcome = state["outcome"]
        tracer.trace.phases.total_us = (time.perf_counter_ns() - state["total_started"]) // 1000
    current_audit().emit(
        "recall",
        "ranked",
        state["outcome"],
        duration_us=(time.perf_counter_ns() - state["total_started"]) // 1000,
        detail={
            "query_hash": hashlib.sha256(state["query"].encode()).hexdigest(),
            "limit": state["limit"],
            "as_of": state["as_of"],
            "intent": state["selected_intent"].value,
            "known_as_of": state["known_as_of"],
            "candidate_limit": state["candidate_limit"],
            "fts_ids": [item["id"] for item in state["fts"]],
            "dense_ids": [item["id"] for item in state["dense"]],
            "rrf_ids": [item["id"] for item in state["ranked_claims"]],
            "returned_ids": [item["id"] for item in final],
            "weights": DEFAULT_WEIGHTS,
            "scores": {
                item["id"]: {
                    **state["feature_by_id"][item["id"]],
                    "pre_rank": state["pre_scores"][item["id"]],
                    "final": (
                        state["rerank_scores"].get(item["id"], state["pre_scores"][item["id"]])
                        if state["reranked"]
                        else state["pre_scores"][item["id"]]
                    ),
                }
                for item in final
            },
            "timing_us": {
                "fts": state["fts_us"],
                "dense": state["dense_us"],
                "reranker": state["rerank_us"],
            },
        },
    )
    for claim in final:
        claim["_score"] = state["rerank_scores"].get(claim["id"], state["pre_scores"][claim["id"]])
    return final
