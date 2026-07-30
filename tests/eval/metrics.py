"""HL-Mem 离线召回指标计算。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean
from typing import Any

from tests.eval.dataset import EvalCase


@dataclass(frozen=True)
class QueryScore:
    """单条评测样本的可审计评分。"""

    case_id: str
    expected_type: str
    returned_count: int
    relevant_count: int
    relevant_hits: int
    hit_at_1: float | None
    hit_at_5: float | None
    recall_at_1: float | None
    recall_at_5: float | None
    precision_at_3: float | None
    top_1_correct: float | None
    keyword_correct: bool
    confidence_correct: bool
    evidence_correct: float | None
    evidence_expected: int
    evidence_hits: int
    stale_hits: int
    temporal_violations: int
    is_empty_prediction: bool
    predicted_no_answer: bool
    low_confidence: bool
    latency_ms: float
    mrr: float | None = None
    ndcg_at_10: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化字典。"""
        return asdict(self)


def compute_mrr(relevant_ids: set[str], results: list[dict]) -> float:
    """计算 MRR：第一个相关结果的倒数排名。"""
    for rank, item in enumerate(results, 1):
        if str(item.get("id")) in relevant_ids:
            return 1.0 / rank
    return 0.0


def compute_binary_ndcg_at_10(relevant_ids: set[str], results: list[dict]) -> float:
    """计算 binary nDCG@10。"""
    import math

    dcg = 0.0
    seen_relevant: set[str] = set()
    for rank, item in enumerate(results[:10], 1):
        claim_id = str(item.get("id"))
        if claim_id in relevant_ids and claim_id not in seen_relevant:
            dcg += 1.0 / math.log2(rank + 1)
            seen_relevant.add(claim_id)
    ideal_hits = min(len(relevant_ids), 10)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def _text(result: dict[str, Any]) -> str:
    value = result.get("text", "")
    return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value


def _temporal_violation(result: dict[str, Any], as_of: str | None) -> bool:
    if not as_of:
        return False
    try:
        reference = datetime.fromisoformat(as_of)
        valid_from = datetime.fromisoformat(result["valid_from"]) if result.get("valid_from") else None
        valid_to = datetime.fromisoformat(result["valid_to"]) if result.get("valid_to") else None
    except (TypeError, ValueError):
        return True
    return bool((valid_from and valid_from > reference) or (valid_to and valid_to <= reference))


def evaluate_results(case: EvalCase, response: dict[str, Any], latency_ms: float = 0.0) -> QueryScore:
    """按样本标签评分一次结构化 recall 响应。"""
    results = response.get("results", [])
    if not isinstance(results, list):
        raise ValueError(f"{case.case_id}: response.results 必须是数组")
    top_five = [item for item in results[:5] if isinstance(item, dict)]
    relevant = set(case.relevant_claim_ids)
    returned_ids = [str(item.get("id")) for item in results if isinstance(item, dict)]
    top_1_hits = relevant.intersection(returned_ids[:1])
    top_3_hits = relevant.intersection(returned_ids[:3])
    top_5_hits = relevant.intersection(returned_ids[:5])
    expected_evidence = set(case.expected_evidence_event_ids)
    returned_evidence = {
        str(link.get("id"))
        for item in top_five
        if str(item.get("id")) in relevant
        for link in item.get("evidence", [])
        if isinstance(link, dict) and link.get("type") == "event"
    }
    text = " ".join(_text(item).casefold() for item in top_five if str(item.get("id")) in relevant)
    keyword_checks = [keyword.casefold() in text for keyword in case.expected_keywords]
    keyword_correct = (
        (all(keyword_checks) if case.keyword_match == "all" else any(keyword_checks)) if keyword_checks else True
    )
    matched = [item for item in top_five if str(item.get("id")) in relevant]
    confidence_correct = all(
        float(item.get("confidence", 0.0)) >= float(case.expected_min_confidence or 0.0) for item in matched
    ) and (bool(matched) or case.expected_type == "empty")
    stale = sum(str(item.get("status")) in case.forbidden_statuses for item in results if isinstance(item, dict))
    temporal = sum(_temporal_violation(item, case.as_of) for item in results if isinstance(item, dict))
    evidence_hits = len(expected_evidence.intersection(returned_evidence))
    evidence_score = (
        evidence_hits / len(returned_evidence) if returned_evidence else (0.0 if expected_evidence else None)
    )
    is_empty = not results
    answerability = str(response.get("answerability") or ("no_evidence" if is_empty else "supported"))
    mrr = compute_mrr(relevant, results) if case.expected_type == "claim" else None
    ndcg = compute_binary_ndcg_at_10(relevant, results) if case.expected_type == "claim" else None
    return QueryScore(
        case_id=case.case_id,
        expected_type=case.expected_type,
        returned_count=len(results),
        relevant_count=len(relevant),
        relevant_hits=len(top_5_hits),
        hit_at_1=float(bool(top_1_hits)) if case.expected_type == "claim" else None,
        hit_at_5=float(bool(top_5_hits)) if case.expected_type == "claim" else None,
        recall_at_1=(len(top_1_hits) / len(relevant) if relevant else 0.0) if case.expected_type == "claim" else None,
        recall_at_5=(len(top_5_hits) / len(relevant) if relevant else 0.0) if case.expected_type == "claim" else None,
        precision_at_3=len(top_3_hits) / 3.0 if case.expected_type == "claim" else None,
        top_1_correct=float(bool(top_1_hits)) if case.expected_type == "claim" else None,
        keyword_correct=keyword_correct,
        confidence_correct=confidence_correct,
        evidence_correct=evidence_score,
        evidence_expected=len(expected_evidence),
        evidence_hits=evidence_hits,
        stale_hits=stale,
        temporal_violations=temporal,
        is_empty_prediction=is_empty,
        predicted_no_answer=answerability == "no_evidence",
        low_confidence=answerability == "low_confidence",
        latency_ms=latency_ms,
        mrr=mrr,
        ndcg_at_10=ndcg,
    )


def _average(values: list[float]) -> float:
    return mean(values) if values else 0.0


def aggregate_metrics(scores: list[QueryScore]) -> dict[str, float]:
    """聚合整套评测的宏观、微观、空答案及正确性指标。"""
    answered = [score for score in scores if score.expected_type == "claim"]
    empty = [score for score in scores if score.expected_type == "empty"]
    predicted_no_answer = [score for score in scores if score.predicted_no_answer]
    correct_no_answer = [score for score in empty if score.predicted_no_answer]
    returned = sum(score.returned_count for score in scores)
    evidence_scores = [score.evidence_correct for score in scores if score.evidence_correct is not None]
    return {
        "hit_at_1": _average([float(score.hit_at_1) for score in answered]),
        "hit_at_5": _average([float(score.hit_at_5) for score in answered]),
        "recall_at_1": _average([float(score.recall_at_1) for score in answered]),
        "recall_at_5": _average([float(score.recall_at_5) for score in answered]),
        "precision_at_3": _average([float(score.precision_at_3) for score in answered]),
        "mrr": _average([float(score.mrr) for score in answered]),
        "ndcg_at_10": _average([float(score.ndcg_at_10) for score in answered]),
        "micro_recall": sum(score.relevant_hits for score in answered)
        / max(1, sum(score.relevant_count for score in answered)),
        "top_1_correctness": _average([float(score.top_1_correct) for score in answered]),
        "no_answer_precision": len(correct_no_answer) / max(1, len(predicted_no_answer)),
        "no_answer_recall": len(correct_no_answer) / max(1, len(empty)),
        "low_confidence_rate": sum(score.low_confidence for score in scores) / max(1, len(scores)),
        "stale_disputed_hit_rate": sum(score.stale_hits for score in scores) / max(1, returned),
        "evidence_correctness": _average([float(value) for value in evidence_scores]),
        "missing_evidence_rate": sum(score.evidence_hits == 0 for score in answered) / max(1, len(answered)),
        "temporal_validity_violation_rate": sum(score.temporal_violations for score in scores) / max(1, returned),
        "mean_latency_ms": _average([score.latency_ms for score in scores]),
    }
