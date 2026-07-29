"""固定快照召回回归评测 runner。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from tests.eval.dataset import bind_cases, load_cases

DEFAULT_DATASET = Path(__file__).parent / "datasets" / "recall_v2.jsonl"
DEFAULT_REPORT = Path(__file__).parent / "reports" / "recall_latest.json"
VALID_RELEVANCE = {"high", "medium", "low", "none"}


def _sha256(path: Path) -> str:
    """计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_rows(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，并兼容旧版关键词绑定标签。"""
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        row.setdefault("id", f"case-{line_number}")
        if not isinstance(row.get("query"), str) or not row["query"].strip():
            raise ValueError(f"第 {line_number} 行 query 无效")
        row.setdefault("expected_claim_ids", [])
        row.setdefault("equivalent_ids", [])
        row.setdefault("forbidden_ids", [])
        row.setdefault("min_relevance", "none" if row.get("expected_type") == "empty" else "high")
        row.setdefault("slice", _legacy_slice(row))
        row.setdefault("notes", "保留的 v2 固定回归样例")
        for field in ("expected_claim_ids", "equivalent_ids", "forbidden_ids"):
            if not isinstance(row[field], list) or any(not isinstance(value, str) for value in row[field]):
                raise ValueError(f"{row['id']}: {field} 必须是字符串列表")
        if row["min_relevance"] not in VALID_RELEVANCE:
            raise ValueError(f"{row['id']}: min_relevance 无效")
        rows.append(row)
    return rows


def _legacy_slice(row: dict[str, Any]) -> str:
    """为保留的 50 条旧样例补充分层标签。"""
    case_id = str(row.get("id", ""))
    query = str(row.get("query", ""))
    if row.get("expected_type") == "empty" or case_id.startswith("N"):
        return "no_answer"
    if row.get("intent") == "historical" or case_id.startswith(("H", "T")):
        return "historical"
    if case_id.startswith("C"):
        return "preference"
    if any(token in query for token in ("那个", "上次", "之前", "后来")):
        return "coreference"
    if any(token in query for token in ("配置", "组合", "缺陷", "规则", "组织")):
        return "broad_topic"
    return "exact_entity"


def _resolve_ids(snapshot: Path, dataset: Path, rows: list[dict[str, Any]]) -> None:
    """用冻结快照解析旧版 binding；显式 ID 始终优先。"""
    if not any(row.get("binding") for row in rows):
        return
    connection = sqlite3.connect(f"file:{snapshot.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        bound = {case.case_id: case for case in bind_cases(connection, load_cases(dataset))}
    finally:
        connection.close()
    for row in rows:
        if not row["expected_claim_ids"] and row["id"] in bound:
            row["expected_claim_ids"] = list(bound[row["id"]].relevant_claim_ids)


def _dcg(hits: list[int]) -> float:
    return sum(hit / math.log2(rank + 1) for rank, hit in enumerate(hits, 1))


def _score(row: dict[str, Any], response: dict[str, Any], latency_ms: float, top_k: int) -> dict[str, Any]:
    """计算一条查询的排序和无答案诊断指标。"""
    results = response.get("results", [])
    if not isinstance(results, list):
        raise ValueError(f"{row['id']}: API results 不是列表")
    returned_ids = [str(item.get("id")) for item in results if isinstance(item, dict)]
    relevant = set(row["expected_claim_ids"]) | set(row["equivalent_ids"])
    forbidden = set(row["forbidden_ids"])
    answerability = str(response.get("answerability") or "supported")
    answerable = row["slice"] != "no_answer" and bool(relevant)
    ranks = [index + 1 for index, claim_id in enumerate(returned_ids) if claim_id in relevant]
    hits5 = [int(claim_id in relevant) for claim_id in returned_ids[:5]]
    ideal = [1] * min(len(relevant), 5)
    return {
        "id": row["id"],
        "slice": row["slice"],
        "answerable": answerable,
        "expected_claim_ids": sorted(relevant),
        "returned_ids": returned_ids[:top_k],
        "recall_at_1": float(bool(returned_ids[:1] and returned_ids[0] in relevant)) if answerable else None,
        "recall_at_5": float(bool(relevant.intersection(returned_ids[:5]))) if answerable else None,
        "mrr": (1.0 / min(ranks) if ranks else 0.0) if answerable else None,
        "ndcg_at_5": (_dcg(hits5) / _dcg(ideal) if ideal else 0.0) if answerable else None,
        "top_3_precision": (sum(claim_id in relevant for claim_id in returned_ids[:3]) / 3.0) if answerable else None,
        "predicted_no_answer": answerability == "no_evidence",
        "low_confidence": answerability == "low_confidence",
        "answerability": answerability,
        "min_relevance": row["min_relevance"],
        "min_relevance_diagnostic": "not yet used for scoring",
        "forbidden_hits": sorted(forbidden.intersection(returned_ids)),
        "latency_ms": latency_ms,
        "http_status": int(response.pop("_http_status", 200)),
    }


def _average(items: list[dict[str, Any]], key: str) -> float:
    values = [float(item[key]) for item in items if item.get(key) is not None]
    return mean(values) if values else 0.0


def _metrics(items: list[dict[str, Any]]) -> dict[str, float]:
    """聚合总体或单 slice 指标。"""
    actual_no_answer = [item for item in items if not item["answerable"]]
    predicted_no_answer = [item for item in items if item["predicted_no_answer"]]
    true_no_answer = [item for item in actual_no_answer if item["predicted_no_answer"]]
    return {
        "recall_at_1": _average(items, "recall_at_1"),
        "recall_at_5": _average(items, "recall_at_5"),
        "mrr": _average(items, "mrr"),
        "ndcg_at_5": _average(items, "ndcg_at_5"),
        "top_3_precision": _average(items, "top_3_precision"),
        "no_answer_precision": len(true_no_answer) / len(predicted_no_answer) if predicted_no_answer else 0.0,
        "no_answer_recall": len(true_no_answer) / len(actual_no_answer) if actual_no_answer else 0.0,
    }


def _percentile(values: list[float], percentile: float) -> float:
    """使用 nearest-rank 计算稳定百分位。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def run(snapshot: Path, dataset: Path, top_k: int) -> dict[str, Any]:
    """在临时数据库上运行评测，保证源 snapshot 只读。"""
    rows = _load_rows(dataset)
    _resolve_ids(snapshot, dataset, rows)
    unresolved = [row["id"] for row in rows if row["slice"] != "no_answer" and not row["expected_claim_ids"]]
    if unresolved:
        raise ValueError(f"可回答样例缺少固定 claim ID/binding: {', '.join(unresolved)}")
    snapshot_hash = _sha256(snapshot)
    scores: list[dict[str, Any]] = []
    reranker_paths: Counter[str] = Counter()
    with tempfile.TemporaryDirectory(prefix="hl-mem-recall-eval-") as temporary_directory:
        working = Path(temporary_directory) / "snapshot.db"
        shutil.copy2(snapshot, working)
        with TestClient(create_app(working)) as client:
            health = client.get("/healthz").json()
            for row in rows:
                payload = {
                    "query": row["query"],
                    "limit": top_k,
                    "intent": row.get("intent", "current_state"),
                    "as_of": row.get("as_of"),
                    "known_as_of": row.get("known_as_of"),
                    "debug": True,
                }
                started = time.perf_counter()
                api_response = client.post("/v1/recall", json=payload)
                latency_ms = (time.perf_counter() - started) * 1000.0
                body = api_response.json()
                body["_http_status"] = api_response.status_code
                trace = body.get("search_trace") or {}
                reranker_paths[str(trace.get("reranker_status", health.get("reranker", "unknown")))] += 1
                scores.append(_score(row, body, latency_ms, top_k))
    if _sha256(snapshot) != snapshot_hash:
        raise RuntimeError("评测期间源 snapshot 发生变化")
    slices: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in scores:
        slices[score["slice"]].append(score)
    latencies = [float(score["latency_ms"]) for score in scores]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": {
            "dataset_sha256": _sha256(dataset),
            "snapshot_sha256": snapshot_hash,
            "dataset": str(dataset.resolve()),
            "snapshot": str(snapshot.resolve()),
        },
        "config": {
            "top_k": top_k,
            "embedder": health.get("embedder", "unknown"),
            "reranker": health.get("reranker", "unknown"),
            "settings": health.get("settings", {}),
            "reranker_paths": dict(reranker_paths),
        },
        "case_count": len(scores),
        "slice_counts": dict(sorted(Counter(score["slice"] for score in scores).items())),
        "metrics": _metrics(scores),
        "slices": {name: {"count": len(items), "metrics": _metrics(items)} for name, items in sorted(slices.items())},
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
        "http_success_rate": sum(score["http_status"] == 200 for score in scores) / len(scores),
        "total_forbidden_hits": sum(len(score["forbidden_hits"]) for score in scores),
        "queries": scores,
    }


def _print_summary(report: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    metrics = report["metrics"]
    print(f"Cases: {report['case_count']} | slices: {report['slice_counts']}")
    print(
        f"Recall@1={metrics['recall_at_1']:.4f} Recall@5={metrics['recall_at_5']:.4f} "
        f"MRR={metrics['mrr']:.4f} nDCG@5={metrics['ndcg_at_5']:.4f} "
        f"Top-3 precision={metrics['top_3_precision']:.4f}"
    )
    print(
        f"No-answer precision={metrics['no_answer_precision']:.4f} "
        f"recall={metrics['no_answer_recall']:.4f} | "
        f"latency p50={report['latency_ms']['p50']:.1f}ms p95={report['latency_ms']['p95']:.1f}ms"
    )
    if baseline and baseline.get("status") == "ready":
        print(
            "Baseline delta: "
            + ", ".join(
                f"{key}={metrics[key] - float(baseline['metrics'][key]):+.4f}"
                for key in ("mrr", "recall_at_5", "no_answer_precision")
            )
        )


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行固定 snapshot 的召回回归评测")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    arguments = parser.parse_args()
    if arguments.top_k < 5:
        parser.error("--top-k 必须至少为 5，才能计算 Recall@5/nDCG@5")
    report = run(arguments.snapshot, arguments.dataset, arguments.top_k)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8")) if arguments.baseline else None
    _print_summary(report, baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
