"""不依赖数据库或第三方库的 benchmark 指标。"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from hl_mem.evaluation.models import GoldTemporal


def _evidence_ids(result: object) -> tuple[str, ...]:
    if isinstance(result, str):
        return (result,)
    if not isinstance(result, Mapping):
        return ()
    evidence = result.get("evidence", ())
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Iterable):
        return ()
    ids: list[str] = []
    for item in evidence:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, Mapping):
            value = item.get("event_id") or item.get("evidence_id") or item.get("id")
            if value is not None:
                ids.append(str(value))
    return tuple(dict.fromkeys(ids))


def recall_at_k(results: Sequence[object], gold_ids: Iterable[str], k: int) -> float:
    """计算 evidence 去重后的 Recall@k。"""
    gold = set(gold_ids)
    if not gold or k <= 0:
        return 0.0
    found = {item for result in results[:k] for item in _evidence_ids(result)}
    return len(found & gold) / len(gold)


def mrr(results: Sequence[object], gold_ids: Iterable[str]) -> float:
    """计算首个相关结果的 reciprocal rank。"""
    gold = set(gold_ids)
    for rank, result in enumerate(results, start=1):
        if set(_evidence_ids(result)) & gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(results: Sequence[object], gold_ids: Iterable[str], k: int) -> float:
    """计算 binary relevance、按 gold evidence 去重的 nDCG@k。"""
    gold = set(gold_ids)
    if not gold or k <= 0:
        return 0.0
    seen: set[str] = set()
    dcg = 0.0
    for rank, result in enumerate(results[:k], start=1):
        relevant = (set(_evidence_ids(result)) & gold) - seen
        if relevant:
            dcg += 1.0 / math.log2(rank + 1)
            seen.update(relevant)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(gold), k) + 1))
    return dcg / ideal


def evidence_precision_recall(
    extracted_claim_evidence_ids: Iterable[str],
    gold_evidence_ids: Iterable[str],
) -> dict[str, float]:
    """计算 extraction evidence precision、recall 与 F1。"""
    extracted, gold = set(extracted_claim_evidence_ids), set(gold_evidence_ids)
    hits = len(extracted & gold)
    precision = hits / len(extracted) if extracted else 0.0
    recall = hits / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def _within(value: str | None, start: str | None, end: str | None) -> bool:
    return value is None or (not start or value >= start) and (not end or value < end)


def _point_within(value: str | None, start: str | None, end: str | None) -> bool:
    """验证事件点时间；gold 有边界时缺失值不能视作正确。"""
    if value is None:
        return start is None and end is None
    return (not start or value >= start) and (not end or value < end)


def temporal_correctness(
    results: Sequence[Mapping[str, Any]],
    gold_temporal: Sequence[GoldTemporal],
) -> dict[str, float | str]:
    """分别评估 valid、occurred、recorded 时间；缺 gold 的维度标记不可用。"""
    gold_by_id = {item.evidence_event_id: item for item in gold_temporal}
    checked = 0
    correct = {"overall": 0, "valid_time": 0, "occurred_time": 0, "recorded_time": 0}
    recorded_checked = 0
    for result in results:
        matching = [gold_by_id[item] for item in _evidence_ids(result) if item in gold_by_id]
        if not matching:
            continue
        checked += 1
        valid_result = False
        occurred_result = False
        recorded_result = False
        recorded_required = False
        for gold in matching:
            valid_ok = _within(result.get("valid_from"), gold.valid_from, gold.valid_to) and _within(
                result.get("valid_to"), gold.valid_from, gold.valid_to
            )
            occurred_value = result.get("occurred_at") or result.get("observed_at")
            occurred_ok = _point_within(occurred_value, gold.occurred_start, gold.occurred_end)
            has_recorded_gold = gold.recorded_from is not None or gold.recorded_to is not None
            recorded_required = recorded_required or has_recorded_gold
            recorded_ok = not has_recorded_gold or (
                _within(result.get("recorded_from"), gold.recorded_from, gold.recorded_to)
                and _within(result.get("recorded_to"), gold.recorded_from, gold.recorded_to)
            )
            valid_result = valid_result or valid_ok
            occurred_result = occurred_result or occurred_ok
            recorded_result = recorded_result or (has_recorded_gold and recorded_ok)
            if valid_ok and occurred_ok and recorded_ok:
                correct["overall"] += 1
                break
        correct["valid_time"] += int(valid_result)
        correct["occurred_time"] += int(occurred_result)
        if recorded_required:
            recorded_checked += 1
            correct["recorded_time"] += int(recorded_result)
    denominator = checked or 1
    return {
        "overall": correct["overall"] / denominator if checked else 0.0,
        "valid_time": correct["valid_time"] / denominator if checked else 0.0,
        "occurred_time": correct["occurred_time"] / denominator if checked else 0.0,
        "recorded_time": (
            correct["recorded_time"] / recorded_checked if recorded_checked else "not_applicable"
        ),
    }


def bootstrap_ci(
    values: Sequence[float],
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float, float]:
    """用固定种子和 1,000 次重采样计算均值的 percentile bootstrap CI。"""
    if not values:
        return (0.0, 0.0)
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    generator = random.Random(seed)
    samples = sorted(
        sum(generator.choice(values) for _ in values) / len(values)
        for _ in range(1000)
    )
    tail = (1.0 - confidence) / 2.0
    lower = samples[max(0, math.floor(tail * len(samples)))]
    upper = samples[min(len(samples) - 1, math.ceil((1.0 - tail) * len(samples)) - 1)]
    return (lower, upper)
