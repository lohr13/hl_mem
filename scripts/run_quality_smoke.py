#!/usr/bin/env python
"""使用确定性本地组件运行最小质量趋势数据集。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.domain.relations import add_relation, get_relations
from hl_mem.evaluation.metrics import mrr, recall_at_k
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "evaluation/datasets/smoke_v1.jsonl"
DEFAULT_RESULTS = ROOT / "evaluation/results"
FIXED_TIME = "2026-01-01T00:00:00+00:00"


def load_cases(path: Path) -> list[dict[str, Any]]:
    """加载并校验 JSONL 冒烟用例。"""
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(cases) != 10:
        raise ValueError(f"quality smoke dataset must contain exactly 10 cases, got {len(cases)}")
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
            "UPDATE claims SET valid_from=?,valid_to=?,status=? WHERE id=?",
            (
                memory.get("valid_from", occurred_at),
                memory.get("valid_to"),
                memory.get("status", "active"),
                stored.claim_id,
            ),
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


def run_case(case: dict[str, Any], database_path: Path) -> dict[str, Any]:
    """在隔离 SQLite 数据库中执行一个用例并返回指标。"""
    database = Database(database_path)
    connection = database.open()
    embedder = FakeEmbedder(dim=64)
    try:
        claim_ids = seed_case(connection, case, embedder)
        if case["type"] == "relation":
            relation = case["input"]["relation"]
            actual = get_relations(connection, claim_ids[str(relation["from_event_id"])], direction="from")
            passed = any(item["relation"] == case["expected"]["relation_type"] for item in actual)
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
        no_match = bool(case["expected"].get("no_match"))
        passed = not results if no_match else recall_10 == 1.0
        item = {
            "id": case["id"],
            "type": case["type"],
            "passed": passed,
            "recall_at_5": recall_5,
            "recall_at_10": recall_10,
            "mrr": reciprocal_rank,
            "returned_ids": [result["id"] for result in results],
        }
        if case["type"] == "temporal":
            item["temporal_accuracy"] = float(passed)
        if case["type"] == "supersede":
            winner = claim_ids[str(case["expected"]["winner_event_id"])]
            item["supersede_accuracy"] = float(bool(results) and results[0]["id"] == winner)
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
        "temporal_accuracy": average("temporal_accuracy", case_results),
        "supersede_accuracy": average("supersede_accuracy", case_results),
        "relation_accuracy": average("relation_accuracy", case_results),
        "case_accuracy": mean(float(item["passed"]) for item in case_results),
    }


def main() -> int:
    """运行数据集并将带时间戳的 JSON 报告写入结果目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()
    cases = load_cases(args.dataset)
    with tempfile.TemporaryDirectory(prefix="hl-mem-smoke-") as temporary:
        root = Path(temporary)
        case_results = [run_case(case, root / f"{case['id']}.sqlite3") for case in cases]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "schema_version": 1,
        "dataset": args.dataset.name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "components": {"extractor": "fake", "embedder": "fake"},
        "metrics": aggregate(case_results),
        "cases": case_results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"smoke_{timestamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "metrics": report["metrics"]}, ensure_ascii=False))
    return 0 if all(item["passed"] for item in case_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
