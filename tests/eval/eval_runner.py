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
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.application.answerability import abstention_kind
from hl_mem.components import make_embedder
from hl_mem.config_loader import load_settings
from hl_mem.core.vector import batch_cosine_similarity
from hl_mem.protocols import embed_queries
from hl_mem.settings import Settings
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


def _sha256_utf8_lf(path: Path) -> str:
    """计算与工作区换行风格无关的 UTF-8/LF 内容摘要。"""
    text = path.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fixture_metadata(snapshot: Path) -> dict[str, str]:
    """读取可选的非生产 CI fixture 标识。"""
    connection = sqlite3.connect(f"file:{snapshot.resolve().as_posix()}?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='eval_fixture_metadata'"
        ).fetchone()
        if not exists:
            return {}
        return {
            str(key): str(value)
            for key, value in connection.execute("SELECT key,value FROM eval_fixture_metadata ORDER BY key")
        }
    finally:
        connection.close()


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


def _score(
    row: dict[str, Any],
    response: dict[str, Any],
    latency_ms: float,
    top_k: int,
    dense_raw_scores: dict[str, float] | None = None,
) -> dict[str, Any]:
    """计算一条查询的排序和无答案诊断指标。"""
    results = response.get("results", [])
    if not isinstance(results, list):
        raise ValueError(f"{row['id']}: API results 不是列表")
    returned_ids = [str(item.get("id")) for item in results if isinstance(item, dict)]
    relevant = set(row["expected_claim_ids"]) | set(row["equivalent_ids"])
    forbidden = set(row["forbidden_ids"])
    trace = response.get("search_trace") or {}
    trace_candidates = trace.get("candidates") or {}
    result_by_id = {
        str(item.get("id")): item for item in results if isinstance(item, dict) and item.get("id") is not None
    }
    raw_scores: list[dict[str, Any]] = []
    for rank, claim_id in enumerate(returned_ids[:top_k], 1):
        candidate = trace_candidates.get(claim_id) if isinstance(trace_candidates, dict) else None
        result = result_by_id.get(claim_id, {})
        reranker_raw_score = candidate.get("rerank_score") if isinstance(candidate, dict) else None
        if reranker_raw_score is None:
            reranker_raw_score = result.get("reranker_raw_score")
        raw_scores.append(
            {
                "claim_id": claim_id,
                "rank": rank,
                "dense_raw_score": (dense_raw_scores or {}).get(claim_id),
                "reranker_raw_score": (
                    float(reranker_raw_score) if isinstance(reranker_raw_score, (int, float)) else None
                ),
            }
        )
    answerability = str(response.get("answerability") or "supported")
    answerability_abstention = abstention_kind(answerability)
    answerable = row["slice"] != "no_answer" and bool(relevant)
    ranks = [index + 1 for index, claim_id in enumerate(returned_ids) if claim_id in relevant]
    top_1_hits = relevant.intersection(returned_ids[:1])
    top_3_hits = relevant.intersection(returned_ids[:3])
    top_5_hits = relevant.intersection(returned_ids[:5])
    seen_relevant: set[str] = set()
    hits5: list[int] = []
    for claim_id in returned_ids[:5]:
        is_new_hit = claim_id in relevant and claim_id not in seen_relevant
        hits5.append(int(is_new_hit))
        if is_new_hit:
            seen_relevant.add(claim_id)
    ideal = [1] * min(len(relevant), 5)
    return {
        "id": row["id"],
        "pair_id": row.get("pair_id"),
        "slice": row["slice"],
        "answerable": answerable,
        "expected_claim_ids": sorted(relevant),
        "returned_ids": returned_ids[:top_k],
        "hit_at_1": float(bool(top_1_hits)) if answerable else None,
        "hit_at_5": float(bool(top_5_hits)) if answerable else None,
        "recall_at_1": len(top_1_hits) / len(relevant) if answerable else None,
        "recall_at_5": len(top_5_hits) / len(relevant) if answerable else None,
        "mrr": (1.0 / min(ranks) if ranks else 0.0) if answerable else None,
        "ndcg_at_5": (_dcg(hits5) / _dcg(ideal) if ideal else 0.0) if answerable else None,
        "precision_at_3": len(top_3_hits) / 3.0 if answerable else None,
        "predicted_no_answer": answerability_abstention != "none",
        "abstention_kind": answerability_abstention,
        "hard_abstention": answerability_abstention == "hard",
        "soft_abstention": answerability_abstention == "soft",
        "low_confidence": answerability == "low_confidence",
        "answerability": answerability,
        "min_relevance": row["min_relevance"],
        "min_relevance_diagnostic": "not yet used for scoring",
        "forbidden_hits": sorted(forbidden.intersection(returned_ids)),
        "raw_scores": raw_scores,
        "expansion_trigger": trace.get("expansion_trigger") if isinstance(trace, dict) else None,
        "expansions": list(trace.get("expansions") or []) if isinstance(trace, dict) else [],
        "latency_ms": latency_ms,
        "http_status": int(response.pop("_http_status", 200)),
    }


def _average(items: list[dict[str, Any]], key: str) -> float:
    values = [float(item[key]) for item in items if item.get(key) is not None]
    return mean(values) if values else 0.0


def _metrics(items: list[dict[str, Any]]) -> dict[str, float]:
    """聚合总体或单 slice 指标。"""
    actual_no_answer = [item for item in items if not item["answerable"]]
    no_answer = _abstention_metrics(items, actual_no_answer, "predicted_no_answer")
    hard = _abstention_metrics(items, actual_no_answer, "hard_abstention")
    soft = _abstention_metrics(items, actual_no_answer, "soft_abstention")
    return {
        "hit_at_1": _average(items, "hit_at_1"),
        "hit_at_5": _average(items, "hit_at_5"),
        "recall_at_1": _average(items, "recall_at_1"),
        "recall_at_5": _average(items, "recall_at_5"),
        "mrr": _average(items, "mrr"),
        "ndcg_at_5": _average(items, "ndcg_at_5"),
        "precision_at_3": _average(items, "precision_at_3"),
        "no_answer_precision": no_answer["precision"],
        "no_answer_recall": no_answer["recall"],
        "no_answer_f1": no_answer["f1"],
        "hard_abstention_precision": hard["precision"],
        "hard_abstention_recall": hard["recall"],
        "hard_abstention_f1": hard["f1"],
        "soft_abstention_precision": soft["precision"],
        "soft_abstention_recall": soft["recall"],
        "soft_abstention_f1": soft["f1"],
        "low_confidence_rate": sum(item["low_confidence"] for item in items) / len(items) if items else 0.0,
    }


def _abstention_metrics(
    items: list[dict[str, Any]],
    actual_no_answer: list[dict[str, Any]],
    prediction_key: str,
) -> dict[str, float]:
    predicted = [item for item in items if item.get(prediction_key)]
    true_positive = [item for item in actual_no_answer if item.get(prediction_key)]
    precision = len(true_positive) / len(predicted) if predicted else 0.0
    recall = len(true_positive) / len(actual_no_answer) if actual_no_answer else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def _percentile(values: list[float], percentile: float) -> float:
    """使用 nearest-rank 计算稳定百分位。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _distribution(values: list[float]) -> dict[str, int | float | None]:
    """汇总用于阈值校准的固定 raw score 分位点。"""
    if not values:
        return {
            "count": 0,
            "min": None,
            "p10": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p90": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(values),
        "min": min(values),
        "p10": _percentile(values, 0.10),
        "p25": _percentile(values, 0.25),
        "p50": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "max": max(values),
        "mean": mean(values),
    }


def _score_distributions(items: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, int | float | None]]]:
    """按查询真值汇总每条查询首个返回 claim 的 raw score。"""
    grouped: dict[str, dict[str, list[float]]] = {
        "answerable": {"dense_raw_score": [], "reranker_raw_score": []},
        "no_answer": {"dense_raw_score": [], "reranker_raw_score": []},
    }
    for item in items:
        raw_scores = item.get("raw_scores") or []
        if not raw_scores:
            continue
        group = "answerable" if item["answerable"] else "no_answer"
        top = raw_scores[0]
        for score_name in ("dense_raw_score", "reranker_raw_score"):
            value = top.get(score_name)
            if isinstance(value, (int, float)):
                grouped[group][score_name].append(float(value))
    return {
        group: {score_name: _distribution(values) for score_name, values in scores.items()}
        for group, scores in grouped.items()
    }


def _dense_raw_scores(
    connection: sqlite3.Connection,
    query_blob: bytes,
    claim_ids: list[str],
) -> dict[str, float]:
    """计算原始 query 与返回 claim 冻结向量间的精确余弦分数。"""
    unique_ids = list(dict.fromkeys(claim_ids))
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    rows = connection.execute(
        f"SELECT id,embedding_dense FROM claims WHERE id IN ({placeholders}) AND embedding_dense IS NOT NULL",
        unique_ids,
    ).fetchall()
    embeddings = {str(claim_id): bytes(blob) for claim_id, blob in rows}
    scored_ids = [claim_id for claim_id in unique_ids if claim_id in embeddings]
    scores = batch_cosine_similarity(query_blob, [embeddings[claim_id] for claim_id in scored_ids])
    return dict(zip(scored_ids, scores, strict=True))


def run(
    snapshot: Path,
    dataset: Path,
    top_k: int,
    settings: Settings | None = None,
    *,
    reference_time: str | None = None,
) -> dict[str, Any]:
    """在临时数据库上运行评测，保证源 snapshot 只读。"""
    rows = _load_rows(dataset)
    _resolve_ids(snapshot, dataset, rows)
    unresolved = [row["id"] for row in rows if row["slice"] != "no_answer" and not row["expected_claim_ids"]]
    if unresolved:
        raise ValueError(f"可回答样例缺少固定 claim ID/binding: {', '.join(unresolved)}")
    snapshot_hash = _sha256(snapshot)
    fixture_metadata = _fixture_metadata(snapshot)
    scores: list[dict[str, Any]] = []
    reranker_paths: Counter[str] = Counter()
    with tempfile.TemporaryDirectory(prefix="hl-mem-recall-eval-") as temporary_directory:
        working = Path(temporary_directory) / "snapshot.db"
        shutil.copy2(snapshot, working)
        runtime_settings = replace(settings or Settings(), database_path=str(working))
        query_blobs = embed_queries(make_embedder(runtime_settings), [str(row["query"]) for row in rows])
        raw_connection = sqlite3.connect(f"file:{working.resolve().as_posix()}?mode=ro", uri=True)
        try:
            with TestClient(create_app(runtime_settings)) as client:
                health = client.get("/healthz").json()
                for row, query_blob in zip(rows, query_blobs, strict=True):
                    payload = {
                        "query": row["query"],
                        "limit": top_k,
                        "intent": row.get("intent", "current_state"),
                        "as_of": row.get("as_of") or reference_time,
                        "known_as_of": row.get("known_as_of"),
                        "namespace": row.get("namespace", "default"),
                        "debug": True,
                    }
                    started = time.perf_counter()
                    api_response = client.post("/v1/recall", json=payload)
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    body = api_response.json()
                    body["_http_status"] = api_response.status_code
                    trace = body.get("search_trace") or {}
                    reranker_paths[str(trace.get("reranker_status", health.get("reranker", "unknown")))] += 1
                    returned_ids = [str(item.get("id")) for item in body.get("results", []) if isinstance(item, dict)]
                    scores.append(
                        _score(
                            row,
                            body,
                            latency_ms,
                            top_k,
                            dense_raw_scores=_dense_raw_scores(raw_connection, query_blob, returned_ids[:top_k]),
                        )
                    )
        finally:
            raw_connection.close()
    if _sha256(snapshot) != snapshot_hash:
        raise RuntimeError("评测期间源 snapshot 发生变化")
    slices: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in scores:
        slices[score["slice"]].append(score)
    latencies = [float(score["latency_ms"]) for score in scores]
    artifacts: dict[str, Any] = {
        "dataset_sha256": _sha256_utf8_lf(dataset),
        "dataset_sha256_algorithm": "sha256-utf8-lf-v1",
        "snapshot_sha256": snapshot_hash,
        "dataset": str(dataset.resolve()),
        "snapshot": str(snapshot.resolve()),
    }
    if fixture_metadata:
        artifacts["fixture"] = fixture_metadata
    return {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
        "config": {
            "top_k": top_k,
            "embedder": health.get("embedder", "unknown"),
            "reranker": health.get("reranker", "unknown"),
            "settings": health.get("settings", {}),
            "reranker_paths": dict(reranker_paths),
            "expansion_triggers": dict(Counter(str(score.get("expansion_trigger") or "none") for score in scores)),
            "raw_dense_score_source": "original_query_cosine",
            "score_distribution_population": "top_returned_claim_per_query",
            "reference_time": reference_time,
        },
        "case_count": len(scores),
        "slice_counts": dict(sorted(Counter(score["slice"] for score in scores).items())),
        "metrics": _metrics(scores),
        "slices": {name: {"count": len(items), "metrics": _metrics(items)} for name, items in sorted(slices.items())},
        "score_distributions": _score_distributions(scores),
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
        "http_success_rate": sum(score["http_status"] == 200 for score in scores) / len(scores),
        "total_forbidden_hits": sum(len(score["forbidden_hits"]) for score in scores),
        "queries": scores,
    }


def _print_summary(report: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    metrics = report["metrics"]
    if baseline and baseline.get("status") == "ready":
        if baseline.get("schema_version") != report.get("schema_version"):
            raise ValueError("baseline schema_version 与报告不一致；请按新指标语义重建 baseline")
    print(f"Cases: {report['case_count']} | slices: {report['slice_counts']}")
    print(
        f"Hit@1={metrics['hit_at_1']:.4f} Hit@5={metrics['hit_at_5']:.4f} "
        f"Recall@1={metrics['recall_at_1']:.4f} Recall@5={metrics['recall_at_5']:.4f} "
        f"MRR={metrics['mrr']:.4f} nDCG@5={metrics['ndcg_at_5']:.4f} "
        f"Precision@3={metrics['precision_at_3']:.4f}"
    )
    print(
        f"No-answer precision={metrics['no_answer_precision']:.4f} "
        f"recall={metrics['no_answer_recall']:.4f} F1={metrics['no_answer_f1']:.4f} | "
        f"hard P/R/F1={metrics['hard_abstention_precision']:.4f}/"
        f"{metrics['hard_abstention_recall']:.4f}/{metrics['hard_abstention_f1']:.4f} | "
        f"soft P/R/F1={metrics['soft_abstention_precision']:.4f}/"
        f"{metrics['soft_abstention_recall']:.4f}/{metrics['soft_abstention_f1']:.4f} | "
        f"low-confidence rate={metrics['low_confidence_rate']:.4f} | "
        f"latency p50={report['latency_ms']['p50']:.1f}ms p95={report['latency_ms']['p95']:.1f}ms"
    )
    for group, distributions in report.get("score_distributions", {}).items():
        for score_name, distribution in distributions.items():
            print(
                f"{group} {score_name}: count={distribution['count']} "
                f"min={distribution['min']} p10={distribution['p10']} p25={distribution['p25']} "
                f"p50={distribution['p50']} p75={distribution['p75']} p90={distribution['p90']} "
                f"max={distribution['max']} mean={distribution['mean']}"
            )
    if baseline and baseline.get("status") == "ready":
        print(
            "Baseline delta: "
            + ", ".join(
                f"{key}={metrics[key] - float(baseline['metrics'][key]):+.4f}"
                for key in ("mrr", "recall_at_5", "no_answer_precision")
            )
        )


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="运行固定 snapshot 的召回回归评测")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expansion-mode", choices=("off", "auto", "always"))
    parser.add_argument("--reference-time")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    arguments = parser.parse_args(argv)
    if arguments.top_k < 5:
        parser.error("--top-k 必须至少为 5，才能计算 Recall@5/nDCG@5")
    settings = load_settings(arguments.config, arguments.env_file)
    if arguments.expansion_mode is not None:
        settings = replace(settings, query_expansion_mode=arguments.expansion_mode)
        settings.validate()
    report = run(
        arguments.snapshot,
        arguments.dataset,
        arguments.top_k,
        settings,
        reference_time=arguments.reference_time,
    )
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8")) if arguments.baseline else None
    _print_summary(report, baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
