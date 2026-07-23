"""记忆召回、混合排序与 observation 失效处理。"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any

from hl_mem.config import RECALL_VECTOR_SCAN_LIMIT
from hl_mem.core.vector import cosine_similarity
from hl_mem.observability.audit import current_audit
from hl_mem.recall.policy import RecallIntent, claim_is_visible, route_recall_intent
from hl_mem.recall.ranking import DEFAULT_WEIGHTS, blend_reranker_score, memory_features, memory_score
from hl_mem.recall.reranker import RerankResult
from hl_mem.recall.trace import SearchTracer
from hl_mem.storage.repository import ClaimRepository, DerivationRepository


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


def _claim_text(claim: dict[str, Any]) -> str:
    return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"


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
    """判断 claim 是否属于偏好属性。"""
    attribute = str(claim.get("canonical_attribute") or "").casefold()
    return "preference" in attribute


def _visibility_filter_reason(
    claim: dict[str, Any],
    reference: str,
    known_as_of: str | None,
    selected_intent: RecallIntent,
) -> str:
    """通过唯一可见性判定函数的反事实调用解释排除阶段。"""
    if known_as_of and claim_is_visible(claim, reference, None, selected_intent):
        return "not_visible_recorded_time"
    active_claim = {**claim, "status": "active"}
    if claim.get("status", "active") != "active" and claim_is_visible(
        active_claim, reference, known_as_of, selected_intent
    ):
        return "status_filtered"
    return "not_visible_valid_time"


def _preference_first(
    claims: list[dict[str, Any]],
    limit: int,
    selected_intent: RecallIntent,
) -> list[dict[str, Any]]:
    """偏好召回时优先保留至多三条可用偏好 claim。"""
    if selected_intent is not RecallIntent.PREFERENCE:
        return claims[:limit]
    preferences = [claim for claim in claims if _is_preference_claim(claim)]
    others = [claim for claim in claims if not _is_preference_claim(claim)]
    reserved = min(3, limit, len(preferences))
    return (preferences[:reserved] + preferences[reserved:] + others)[:limit]


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
    tracer: SearchTracer | None = None,
) -> list[dict[str, Any]]:
    """融合全文、向量、多因子先验及 reranker 结果召回 claim。"""
    audit = current_audit()
    total_started = time.perf_counter_ns()
    candidate_limit = min(RECALL_VECTOR_SCAN_LIMIT, max(limit * 5, 50))
    ranking_now = now or datetime.now(timezone.utc).isoformat()
    selected_intent = RecallIntent(intent) if intent else route_recall_intent(query, as_of, ranking_now)
    reference = as_of or ranking_now
    started = time.perf_counter_ns()
    try:
        fts = repo.search_claims_fts(
            query, candidate_limit, reference, selected_intent, known_as_of, namespace=namespace
        )
    except TypeError:
        fts = repo.search_claims_fts(query, candidate_limit, as_of)
    fts_us = (time.perf_counter_ns() - started) // 1000
    if tracer is not None:
        tracer.trace.candidate_limit = candidate_limit
        tracer.trace.phases.fts_us = fts_us
        tracer.record_channel("fts", fts)
    started = time.perf_counter_ns()
    if hasattr(repo, "search_claims_vector"):
        try:
            dense = repo.search_claims_vector(
                query_blob, candidate_limit, reference, selected_intent, known_as_of, namespace=namespace
            )
        except TypeError:
            dense = repo.search_claims_vector(query_blob, candidate_limit, as_of)
    else:
        try:
            embedded = repo.list_embedded(as_of, namespace=namespace)
        except TypeError:
            embedded = repo.list_embedded(as_of)
        dense = sorted(
            embedded,
            key=lambda claim: cosine_similarity(query_blob, claim["embedding_dense"]),
            reverse=True,
        )[:candidate_limit]
    dense_us = (time.perf_counter_ns() - started) // 1000
    if tracer is not None:
        tracer.trace.phases.dense_us = dense_us
        tracer.record_channel("dense", dense)
    fusion_started = time.perf_counter_ns()
    scores: dict[str, float] = {}
    visible: list[dict[str, Any]] = []
    for claim in fts + dense:
        if claim_is_visible(claim, reference, known_as_of, selected_intent):
            visible.append(claim)
        elif tracer is not None:
            tracer.record_filter(
                str(claim["id"]),
                _visibility_filter_reason(claim, reference, known_as_of, selected_intent),
            )
    by_id = {claim["id"]: claim for claim in visible}
    helpful_rates = repo.helpful_rates(list(by_id)) if hasattr(repo, "helpful_rates") else {}
    for claim_id, helpful_rate in helpful_rates.items():
        by_id[claim_id]["helpful_rate"] = helpful_rate
    for ranked in (fts, dense):
        for rank, claim in enumerate(ranked, 1):
            scores[claim["id"]] = scores.get(claim["id"], 0) + 1 / (60 + rank)
    max_access = max((_access_count(claim) for claim in by_id.values()), default=0)
    feature_by_id = {
        claim_id: memory_features(claim, scores[claim_id] / (2 / 61), max_access, ranking_now)
        for claim_id, claim in by_id.items()
    }
    pre_scores = {
        claim_id: memory_score(features)
        + (
            0.12 * features["recency"]
            if selected_intent is RecallIntent.PREFERENCE and _is_preference_claim(by_id[claim_id])
            else 0.0
        )
        for claim_id, features in feature_by_id.items()
    }
    ranked_claims = sorted(
        by_id.values(),
        key=lambda claim: (
            -pre_scores[claim["id"]],
            -feature_by_id[claim["id"]]["semantic"],
            -_recorded_epoch(claim),
            str(claim["id"]),
        ),
    )
    if tracer is not None:
        tracer.trace.phases.fusion_us = (time.perf_counter_ns() - fusion_started) // 1000
        tracer.record_pre_rank(ranked_claims, pre_scores)
    rerank_us = 0
    reranked: list[tuple[int, float]] = []
    rerank_scores: dict[str, float] = {}
    if reranker is None:
        outcome, final = "disabled", _preference_first(ranked_claims, limit, selected_intent)
    elif len(ranked_claims) <= 1:
        outcome, final = "skipped", _preference_first(ranked_claims, limit, selected_intent)
    else:
        candidates = ranked_claims[:candidate_limit]
        started = time.perf_counter_ns()
        returned = reranker.rerank(query, [_claim_text(claim) for claim in candidates], top_n=candidate_limit)
        rerank_us = (time.perf_counter_ns() - started) // 1000
        if tracer is not None:
            tracer.trace.phases.reranker_us = rerank_us
        if isinstance(returned, RerankResult):
            reranked, result_status = returned.results, returned.outcome
        else:
            reranked = returned
            last = getattr(reranker, "last_outcome", None)
            result_status = getattr(last, "outcome", None) or last or ("empty" if not reranked else "success")
        if reranked:
            valid = [(candidates[index], score) for index, score in reranked if 0 <= index < len(candidates)]
            raw_rerank_scores = {claim["id"]: float(score) for claim, score in valid}
            if tracer is not None:
                tracer.record_rerank([(str(claim["id"]), float(score)) for claim, score in valid])
            rerank_scores = {
                claim["id"]: blend_reranker_score(score, feature_by_id[claim["id"]]) for claim, score in valid
            }
            reranked_claims = sorted(
                (claim for claim, _ in valid),
                key=lambda claim: (
                    -rerank_scores[claim["id"]],
                    -raw_rerank_scores[claim["id"]],
                    -feature_by_id[claim["id"]]["semantic"],
                    -_recorded_epoch(claim),
                    str(claim["id"]),
                ),
            )
            if selected_intent is RecallIntent.PREFERENCE:
                reranked_ids = {claim["id"] for claim in reranked_claims}
                reranked_claims.extend(
                    claim
                    for claim in ranked_claims
                    if _is_preference_claim(claim) and claim["id"] not in reranked_ids
                )
            final = _preference_first(reranked_claims, limit, selected_intent)
            outcome = "applied"
        else:
            outcome = "error_fallback" if result_status == "error" else "empty_fallback"
            final = _preference_first(ranked_claims, limit, selected_intent)
    if tracer is not None:
        final_ids = {str(claim["id"]) for claim in final}
        if reranked:
            reranked_ids = {str(claim["id"]) for claim, _ in valid}
            for claim in ranked_claims:
                if str(claim["id"]) not in reranked_ids:
                    tracer.record_filter(str(claim["id"]), "reranker_omitted")
        for claim in ranked_claims:
            claim_id = str(claim["id"])
            if claim_id not in final_ids and claim_id in tracer.trace.candidates:
                tracer.record_filter(claim_id, "final_limit")
        tracer.record_final(final)
        tracer.trace.outcome = outcome
        tracer.trace.phases.total_us = (time.perf_counter_ns() - total_started) // 1000
    audit.emit(
        "recall",
        "ranked",
        outcome,
        duration_us=(time.perf_counter_ns() - total_started) // 1000,
        detail={
            "query_hash": hashlib.sha256(query.encode()).hexdigest(),
            "limit": limit,
            "as_of": as_of,
            "intent": selected_intent.value,
            "known_as_of": known_as_of,
            "candidate_limit": candidate_limit,
            "fts_ids": [item["id"] for item in fts],
            "dense_ids": [item["id"] for item in dense],
            "rrf_ids": [item["id"] for item in ranked_claims],
            "returned_ids": [item["id"] for item in final],
            "weights": DEFAULT_WEIGHTS,
            "scores": {
                item["id"]: {
                    **feature_by_id[item["id"]],
                    "pre_rank": pre_scores[item["id"]],
                    "final": (
                        rerank_scores.get(item["id"], pre_scores[item["id"]]) if reranked else pre_scores[item["id"]]
                    ),
                }
                for item in final
            },
            "timing_us": {"fts": fts_us, "dense": dense_us, "reranker": rerank_us},
        },
    )
    for claim in final:
        claim["_score"] = rerank_scores.get(claim["id"], pre_scores[claim["id"]])
    return final


def stale_observations(connection: Any, claim_id: str, commit: bool = True) -> None:
    """将依赖指定 claim 的 observation 标记为过期。"""
    rows = connection.execute(
        "SELECT derived_id FROM evidence_links WHERE derived_type='observation' "
        "AND evidence_type='claim' AND evidence_id=?",
        (claim_id,),
    ).fetchall()
    for row in rows:
        DerivationRepository(connection).update_status(row["derived_id"], "stale", commit=commit)
