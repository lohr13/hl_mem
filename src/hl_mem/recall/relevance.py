"""召回候选 relevance gate 的诊断评估与连续尾部截断。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from hl_mem.recall.trace import CandidateTrace, SearchTracer

RelevanceDecisionName = Literal["relevant", "borderline", "irrelevant"]

_BORDERLINE_TOLERANCE = 0.05


def should_enforce_relevance(
    gate_mode: str,
    intent: str,
    allowed_intents: tuple[str, ...],
) -> bool:
    """仅在显式 enforce 且 intent 位于白名单时启用截断。"""
    return gate_mode == "enforce" and intent in allowed_intents


@dataclass(frozen=True)
class RelevanceDecision:
    """描述单个候选的 relevance 判定及其依据。"""

    decision: RelevanceDecisionName
    reason: str
    score_path: str
    evidence_score: float | None
    relative_drop: float | None = None


def _fallback_decision(trace: CandidateTrace, dense_floor: float) -> RelevanceDecision:
    """按通道证据组合评估 reranker fallback 候选。"""
    channels = set(trace.channels)
    dense_score = trace.channel_scores.get("dense")
    if len(channels) >= 2:
        return RelevanceDecision("relevant", "multi_channel_hit", "reranker_fallback", dense_score)
    if "fts" in channels and dense_score is not None and dense_score >= dense_floor:
        return RelevanceDecision("relevant", "fts_dense_supported", "reranker_fallback", dense_score)
    if (
        "fts" in channels
        and dense_score is not None
        and dense_floor - _BORDERLINE_TOLERANCE <= dense_score < dense_floor
    ):
        return RelevanceDecision("borderline", "below_dense_floor", "reranker_fallback", dense_score)
    return RelevanceDecision("irrelevant", "below_dense_floor", "reranker_fallback", dense_score)


def _candidate_decision(
    trace: CandidateTrace,
    reranker_floor: float,
    dense_floor: float,
) -> RelevanceDecision:
    """根据实际评分路径选择不可混用的 relevance 规则。"""
    if trace.rerank_score is None:
        return _fallback_decision(trace, dense_floor)
    if trace.rerank_score >= reranker_floor:
        return RelevanceDecision(
            "relevant",
            "reranker_floor_met",
            "reranker_applied",
            trace.rerank_score,
        )
    decision: RelevanceDecisionName = (
        "borderline" if reranker_floor - _BORDERLINE_TOLERANCE <= trace.rerank_score < reranker_floor else "irrelevant"
    )
    return RelevanceDecision(
        decision,
        "below_reranker_floor",
        "reranker_applied",
        trace.rerank_score,
    )


def evaluate_relevance(
    claim_ids: list[str],
    tracer: SearchTracer,
    *,
    reranker_floor: float,
    dense_floor: float,
    relative_drop_threshold: float,
) -> dict[str, RelevanceDecision]:
    """按当前结果顺序计算诊断，不修改候选、分数或排序。"""
    decisions: dict[str, RelevanceDecision] = {}
    for claim_id in claim_ids:
        candidate = tracer.trace.candidates.get(claim_id)
        if candidate is None:
            continue
        decision = _candidate_decision(candidate, reranker_floor, dense_floor)
        decisions[claim_id] = decision
        tracer.record_relevance(claim_id, decision)
        if decision.decision != "relevant":
            tracer.record_filter(claim_id, decision.reason)

    if len(claim_ids) >= 2:
        top = decisions.get(claim_ids[0])
        second = decisions.get(claim_ids[1])
        if (
            top is not None
            and second is not None
            and top.score_path == second.score_path
            and top.evidence_score is not None
            and second.evidence_score is not None
        ):
            denominator = max(abs(top.evidence_score), 1e-12)
            relative_drop = max(0.0, (top.evidence_score - second.evidence_score) / denominator)
            tracer.trace.candidates[claim_ids[1]].relative_drop = relative_drop
            decisions[claim_ids[1]] = RelevanceDecision(
                second.decision,
                second.reason,
                second.score_path,
                second.evidence_score,
                relative_drop,
            )
            if relative_drop >= relative_drop_threshold:
                tracer.record_filter(claim_ids[1], "relative_score_drop")

    if decisions and not any(item.decision == "relevant" for item in decisions.values()):
        for claim_id in decisions:
            tracer.record_filter(claim_id, "query_no_evidence")
    return decisions


def enforce_relevance(
    claims: list[dict[str, Any]],
    tracer: SearchTracer,
    *,
    reranker_floor: float,
    dense_floor: float,
    relative_drop_threshold: float,
    keep_top1: bool,
) -> list[dict[str, Any]]:
    """按候选顺序执行不可逆 relevance 截断并返回保留的连续前缀。"""
    claim_ids = [str(claim["id"]) for claim in claims]
    decisions = evaluate_relevance(
        claim_ids,
        tracer,
        reranker_floor=reranker_floor,
        dense_floor=dense_floor,
        relative_drop_threshold=relative_drop_threshold,
    )
    retained: list[dict[str, Any]] = []
    previous_decision: RelevanceDecision | None = None

    for index, claim in enumerate(claims):
        claim_id = claim_ids[index]
        decision = decisions.get(claim_id)
        candidate = tracer.trace.candidates.get(claim_id)
        has_basic_signal = bool(candidate and (candidate.channels or candidate.rerank_score is not None))
        if decision is None or not has_basic_signal:
            tracer.record_filter(claim_id, "query_no_evidence")
            break

        if index == 0 and keep_top1:
            retained.append(claim)
            previous_decision = decision
            continue

        if decision.decision != "relevant":
            break

        if (
            previous_decision is not None
            and previous_decision.score_path == decision.score_path
            and previous_decision.evidence_score is not None
            and decision.evidence_score is not None
        ):
            denominator = max(abs(previous_decision.evidence_score), 1e-12)
            relative_drop = max(
                0.0,
                (previous_decision.evidence_score - decision.evidence_score) / denominator,
            )
            if candidate is not None:
                candidate.relative_drop = relative_drop
            if relative_drop > relative_drop_threshold:
                tracer.record_filter(claim_id, "relative_score_drop")
                break

        retained.append(claim)
        previous_decision = decision

    tracer.record_final(retained)
    return retained
