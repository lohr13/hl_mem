"""固定召回回归集的离线指标计算入口。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

EVALUATION_ROOT = Path(__file__).resolve().parents[1]


def evaluate(cases: list[dict[str, Any]], predictions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """计算排序、no-answer、覆盖率、HTTP、延迟和降级指标。"""
    reciprocal_ranks: list[float] = []
    hit_at_1 = hit_at_5 = recall_at_1 = recall_at_5 = precision_at_3 = 0.0
    retrieval_cases = answered = http_success = 0
    true_no_answer = predicted_no_answer = correct_no_answer = 0
    low_confidence = 0
    latencies: list[float] = []
    degradations: dict[str, int] = {}
    for case in cases:
        prediction = predictions.get(case["id"], {})
        ids = list(prediction.get("result_ids", []))
        gold = set(case["gold_ids"])
        expected_no = bool(case["no_answer"])
        if not expected_no and gold:
            retrieval_cases += 1
            top_1_hits = gold.intersection(ids[:1])
            top_3_hits = gold.intersection(ids[:3])
            top_5_hits = gold.intersection(ids[:5])
            rank = next((index for index, item_id in enumerate(ids, 1) if item_id in gold), 0)
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            hit_at_1 += bool(top_1_hits)
            hit_at_5 += bool(top_5_hits)
            recall_at_1 += len(top_1_hits) / len(gold)
            recall_at_5 += len(top_5_hits) / len(gold)
            precision_at_3 += len(top_3_hits) / 3.0
        answered += bool(ids)
        answerability = prediction.get("answerability")
        actual_no = answerability == "no_evidence"
        low_confidence += answerability == "low_confidence"
        true_no_answer += expected_no
        predicted_no_answer += actual_no
        correct_no_answer += expected_no and actual_no
        http_success += int(prediction.get("http_status", 0)) < 400 and bool(prediction)
        if "latency_ms" in prediction:
            latencies.append(float(prediction["latency_ms"]))
        degradation = prediction.get("degradation")
        if degradation:
            degradations[str(degradation)] = degradations.get(str(degradation), 0) + 1

    def percentile(value: float) -> float:
        values = sorted(latencies)
        return values[min(len(values) - 1, math.ceil(value * len(values)) - 1)] if values else 0.0

    count = max(1, len(cases))
    retrieval_count = max(1, retrieval_cases)
    return {
        "schema_version": 2,
        "hit_at_1": hit_at_1 / retrieval_count,
        "hit_at_5": hit_at_5 / retrieval_count,
        "recall_at_1": recall_at_1 / retrieval_count,
        "recall_at_5": recall_at_5 / retrieval_count,
        "precision_at_3": precision_at_3 / retrieval_count,
        "mrr": sum(reciprocal_ranks) / retrieval_count,
        "no_answer_precision": correct_no_answer / max(1, predicted_no_answer),
        "no_answer_recall": correct_no_answer / max(1, true_no_answer),
        "low_confidence_rate": low_confidence / count,
        "coverage": answered / count,
        "http_success": http_success / count,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "degradation_rates": {key: value / count for key, value in degradations.items()},
    }


def main() -> None:
    """读取 JSONL 数据集及可选预测文件并打印 JSON 指标。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=EVALUATION_ROOT / "datasets" / "recall_regression_v1.jsonl",
    )
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    predictions = json.loads(args.predictions.read_text(encoding="utf-8")) if args.predictions else {}
    print(json.dumps(evaluate(cases, predictions), ensure_ascii=False))


if __name__ == "__main__":
    main()
