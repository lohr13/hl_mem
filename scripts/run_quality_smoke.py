#!/usr/bin/env python
"""使用确定性本地组件运行最小质量趋势数据集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.domain.relations import add_relation, get_relations
from hl_mem.evaluation.metrics import mrr, recall_at_k
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.protocols import ClaimRow, RelationProposal
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.discover_relations import discover_relations

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "evaluation/datasets/smoke_v2.jsonl"
DEFAULT_BASELINE = ROOT / "evaluation/baselines/smoke_v2_baseline.json"
DEFAULT_RESULTS = ROOT / "evaluation/results"
FIXED_TIME = "2026-01-01T00:00:00+00:00"
DEFAULT_TOLERANCES = {
    "recall_at_5": 0.05,
    "recall_at_10": 0.1,
    "mrr": 0.05,
    "precision_at_5": 0.1,
    "temporal_accuracy": 0.0,
    "supersede_accuracy": 0.0,
    "relation_accuracy": 0.0,
    "case_accuracy": 0.0,
}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """加载并校验 JSONL 冒烟用例。"""
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not 15 <= len(cases) <= 20:
        raise ValueError(f"quality smoke dataset must contain 15-20 cases, got {len(cases)}")
    required = {"id", "type", "input", "expected"}
    for index, case in enumerate(cases, 1):
        missing = required - case.keys()
        if missing:
            raise ValueError(f"case {index} is missing fields: {', '.join(sorted(missing))}")
    return cases


def seed_case(connection: Any, case: dict[str, Any], embedder: FakeEmbedder) -> dict[str, str]:
    """通过 FakeExtractor 和 FakeEmbedder 写入单个用例的记忆。"""
    extractor = FakeExtractor()
    claim_ids: dict[str, str] = {}
    connection.commit()
    for memory in case["input"].get("memories", []):
        event_id = str(memory["id"])
        occurred_at = str(memory.get("occurred_at", FIXED_TIME))
        event = {
            "id": event_id,
            "idempotency_key": f"smoke:{case['id']}:{event_id}",
            "tenant_id": "default",
            "event_type": "message",
            "actor_type": "user",
            "content": {"text": str(memory["text"])},
            "occurred_at": occurred_at,
            "recorded_at": occurred_at,
        }
        IngestService(connection).ingest_event(event, event["idempotency_key"])
        extracted = extractor.extract(event["content"])
        if len(extracted) != 1:
            raise ValueError(f"case {case['id']} memory {event_id} must produce exactly one fake extraction")
        stored = IngestService.store_extracted(connection, extracted[0], event, occurred_at, embedder)
        claim_ids[event_id] = stored.claim_id
        connection.execute(
            "UPDATE claims SET valid_from=?,valid_to=? WHERE id=?",
            (memory.get("valid_from", occurred_at), memory.get("valid_to"), stored.claim_id),
        )
        connection.commit()
    relation = case["input"].get("relation")
    if relation:
        add_relation(
            connection,
            claim_ids[str(relation["from_event_id"])],
            claim_ids[str(relation["to_event_id"])],
            str(relation["type"]),
        )
    connection.commit()
    return claim_ids


class FakeRelationDiscoverer:
    """按用例声明返回确定性关系提案，不调用 LLM 或网络。"""

    def __init__(self, from_claim_id: str, to_claim_id: str, relation: str, confidence: float) -> None:
        self.from_claim_id = from_claim_id
        self.to_claim_id = to_claim_id
        self.relation = relation
        self.confidence = confidence

    def propose(
        self,
        source_claim: ClaimRow,
        candidates: list[ClaimRow],
        *,
        max_proposals: int,
    ) -> list[RelationProposal]:
        """仅当声明的目标位于真实候选池时生成一条提案。"""
        candidate_ids = {str(candidate["id"]) for candidate in candidates}
        if str(source_claim["id"]) != self.from_claim_id or self.to_claim_id not in candidate_ids or max_proposals < 1:
            return []
        return [
            RelationProposal(
                from_claim_id=self.from_claim_id,
                to_claim_id=self.to_claim_id,
                relation=self.relation,
                confidence=self.confidence,
                rationale="deterministic smoke relation",
                supporting_claim_ids=(self.from_claim_id, self.to_claim_id),
                model="fake-relation-v1",
            )
        ]


def run_case(case: dict[str, Any], database_path: Path) -> dict[str, Any]:
    """在隔离 SQLite 数据库中执行一个用例并返回指标。"""
    database = Database(database_path)
    connection = database.open()
    embedder = FakeEmbedder(dim=64)
    try:
        claim_ids = seed_case(connection, case, embedder)
        if case["type"] == "relation_storage":
            relation = case["input"]["relation"]
            actual = get_relations(connection, claim_ids[str(relation["from_event_id"])], direction="from")
            passed = any(item["relation"] == case["expected"]["relation_type"] for item in actual)
            return {"id": case["id"], "type": case["type"], "passed": passed, "relation_accuracy": float(passed)}
        if case["type"] == "relation_discovery":
            relation = case["input"]["discovery"]
            from_claim_id = claim_ids[str(relation["from_event_id"])]
            to_claim_id = claim_ids[str(relation["to_event_id"])]
            counts = discover_relations(
                connection,
                FakeRelationDiscoverer(
                    from_claim_id,
                    to_claim_id,
                    str(relation["type"]),
                    float(relation["confidence"]),
                ),
                from_claim_id,
                mode="auto",
                pool_limit=10,
                max_proposals=1,
                auto_apply_confidence=0.9,
                conflict_confidence=0.9,
            )
            actual = get_relations(connection, from_claim_id, direction="from")
            passed = counts["applied"] == 1 and any(
                item["relation"] == case["expected"]["relation_type"] for item in actual
            )
            return {"id": case["id"], "type": case["type"], "passed": passed, "relation_accuracy": float(passed)}

        settings = Settings(embedder_mode="fake", embedding_dim=64, reranker_mode="off")
        response = RecallService(connection, embedder, settings=settings).recall(
            str(case["input"]["query"]),
            limit=10,
            as_of=case["input"].get("as_of"),
            namespace="default",
        )
        results = response["results"]
        expected_ids = list(case["expected"].get("event_ids", []))
        recall_5 = recall_at_k(results, expected_ids, 5)
        recall_10 = recall_at_k(results, expected_ids, 10)
        reciprocal_rank = mrr(results, expected_ids)
        relevant_in_top_5 = sum(
            bool(
                {
                    str(item.get("event_id") or item.get("evidence_id") or item.get("id"))
                    for item in result.get("evidence", [])
                }
                & set(expected_ids)
            )
            for result in results[:5]
        )
        precision_5 = relevant_in_top_5 / min(5, len(results)) if results else 0.0
        no_match = bool(case["expected"].get("no_match"))
        max_rank = case["expected"].get("max_rank")
        rank_constraint_passed = True
        if case["type"] == "recall":
            if not isinstance(max_rank, int) or max_rank < 1:
                raise ValueError(f"recall case {case['id']} must define a positive max_rank")
            top_ranked_evidence = {
                str(evidence.get("event_id") or evidence.get("evidence_id") or evidence.get("id"))
                for result in results[:max_rank]
                for evidence in result.get("evidence", [])
            }
            rank_constraint_passed = set(expected_ids).issubset(top_ranked_evidence)
        passed = not results if no_match else recall_10 == 1.0 and rank_constraint_passed
        item = {
            "id": case["id"],
            "type": case["type"],
            "passed": passed,
            "recall_at_5": recall_5,
            "recall_at_10": recall_10,
            "mrr": reciprocal_rank,
            "precision_at_5": precision_5,
            "max_rank": max_rank,
            "rank_constraint_passed": rank_constraint_passed,
            "returned_ids": [result["id"] for result in results],
        }
        if case["type"] == "temporal":
            item["temporal_accuracy"] = float(passed)
        if case["type"] == "supersede":
            winner = claim_ids[str(case["expected"]["winner_event_id"])]
            superseded = claim_ids[str(case["expected"]["superseded_event_id"])]
            winner_row = ClaimRepository(connection).get_claim(winner)
            superseded_row = ClaimRepository(connection).get_claim(superseded)
            lifecycle_correct = bool(
                winner_row
                and superseded_row
                and winner_row["status"] == "active"
                and superseded_row["status"] == "superseded"
                and superseded_row["superseded_by_id"] == winner
            )
            winner_recalled = any(result["id"] == winner for result in results)
            item["supersede_accuracy"] = float(lifecycle_correct and winner_recalled)
            item["passed"] = bool(item["supersede_accuracy"])
        return item
    finally:
        database.close()


def aggregate(case_results: list[dict[str, Any]]) -> dict[str, float]:
    """聚合质量趋势所需的固定指标。"""
    retrieval = [item for item in case_results if "recall_at_5" in item and item["type"] != "negative"]

    def average(key: str, items: list[dict[str, Any]]) -> float:
        values = [float(item[key]) for item in items if key in item]
        return mean(values) if values else 0.0

    return {
        "recall_at_5": average("recall_at_5", retrieval),
        "recall_at_10": average("recall_at_10", retrieval),
        "mrr": average("mrr", retrieval),
        "precision_at_5": average("precision_at_5", retrieval),
        "temporal_accuracy": average("temporal_accuracy", case_results),
        "supersede_accuracy": average("supersede_accuracy", case_results),
        "relation_accuracy": average("relation_accuracy", case_results),
        "case_accuracy": mean(float(item["passed"]) for item in case_results),
    }


def dataset_hash(path: Path) -> str:
    """返回数据集内容的 SHA-256 摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], percentile_rank: float) -> float:
    """使用线性插值计算延迟分位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_rank
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * weight


def case_metrics(case_results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """提取适合持久化比较的逐用例数值指标。"""
    return {
        str(item["id"]): {
            key: float(value)
            for key, value in item.items()
            if key not in {"id", "type", "passed", "returned_ids", "latency_ms", "max_rank", "rank_constraint_passed"}
            and isinstance(value, (int, float))
        }
        for item in case_results
    }


def write_baseline(path: Path, source_hash: str, metrics: dict[str, float], cases: dict[str, dict[str, float]]) -> None:
    """显式写入当前 smoke 指标，作为后续质量退化门禁。"""
    payload = {
        "schema_version": 1,
        "dataset_hash": source_hash,
        "metrics": metrics,
        "expected_metrics_per_case": cases,
        "tolerance_thresholds": DEFAULT_TOLERANCES,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compare_baseline(
    path: Path,
    source_hash: str,
    current_metrics: dict[str, float],
    current_cases: dict[str, dict[str, float]],
) -> tuple[dict[str, Any], dict[str, float], dict[str, dict[str, float]], bool]:
    """比较聚合和逐用例指标；任何超出阈值的下降都会关闭门禁。"""
    baseline = json.loads(path.read_text(encoding="utf-8"))
    tolerances = {key: float(value) for key, value in baseline["tolerance_thresholds"].items()}
    baseline_metrics = {key: float(value) for key, value in baseline["metrics"].items()}
    aggregate_delta = {key: current_metrics[key] - value for key, value in baseline_metrics.items()}
    aggregate_passed = all(delta >= -tolerances.get(key, 0.0) for key, delta in aggregate_delta.items())

    expected_cases = baseline["expected_metrics_per_case"]
    cases_delta: dict[str, dict[str, float]] = {}
    cases_passed = set(current_cases) == set(expected_cases)
    for case_id, expected in expected_cases.items():
        actual = current_cases.get(case_id)
        if actual is None or set(actual) != set(expected):
            cases_passed = False
            continue
        deltas = {key: actual[key] - float(value) for key, value in expected.items()}
        cases_delta[case_id] = deltas
        if any(delta < -tolerances.get(key, 0.0) for key, delta in deltas.items()):
            cases_passed = False
    hash_matches = baseline.get("dataset_hash") == source_hash
    return baseline, aggregate_delta, cases_delta, bool(hash_matches and aggregate_passed and cases_passed)


def main() -> int:
    """运行数据集并将带时间戳的 JSON 报告写入结果目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    with tempfile.TemporaryDirectory(prefix="hl-mem-smoke-") as temporary:
        root = Path(temporary)
        case_results = []
        for case in cases:
            started_at = perf_counter()
            case_result = run_case(case, root / f"{case['id']}.sqlite3")
            case_result["latency_ms"] = (perf_counter() - started_at) * 1000.0
            case_results.append(case_result)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    metrics = aggregate(case_results)
    latencies = [float(item["latency_ms"]) for item in case_results]
    latency = {"p50_ms": percentile(latencies, 0.5), "p90_ms": percentile(latencies, 0.9)}
    per_case = case_metrics(case_results)
    source_hash = dataset_hash(args.dataset)
    if args.update_baseline:
        write_baseline(args.baseline, source_hash, metrics, per_case)
    if not args.baseline.is_file():
        raise FileNotFoundError(f"quality smoke baseline not found: {args.baseline}")
    baseline, aggregate_delta, cases_delta, baseline_passed = compare_baseline(
        args.baseline, source_hash, metrics, per_case
    )
    cases_passed = all(item["passed"] for item in case_results)
    passed = cases_passed and baseline_passed
    report = {
        "schema_version": 1,
        "dataset": args.dataset.name,
        "dataset_hash": source_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "components": {"extractor": "fake", "embedder": "fake"},
        "metrics": metrics,
        "latency": latency,
        "cases": case_results,
        "baseline_comparison": {
            "baseline": baseline["metrics"],
            "delta": aggregate_delta,
            "case_delta": cases_delta,
            "passed": baseline_passed,
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"smoke_{timestamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "current": metrics,
                "latency": latency,
                "baseline": baseline["metrics"],
                "delta": aggregate_delta,
                "passed": passed,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
