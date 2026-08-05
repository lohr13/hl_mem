"""Calibrate a Q2 no-answer gate on the frozen recall evaluation set.

This is an offline analysis script.  It does not start the service and does not
write to the production database.  The default corpus manifest is the backup
captured immediately before the Q2 migration, so the intended 1,131-claim
snapshot remains reproducible even if the live service ingests new claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for import_path in (str(SRC), str(SCRIPTS)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

from run_embedding_ablation import (  # noqa: E402
    CONFIGS,
    DashScopeEmbeddingClient,
    _load_env_value,
    _normalize_rows,
    embed_remote,
)

from hl_mem.recall.reranker import DashScopeReranker  # noqa: E402

DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "recall_eval_v1.jsonl"
DEFAULT_DATABASE = ROOT / "var" / "hl_mem.db"
DEFAULT_MANIFEST_DATABASE = ROOT / "var" / "hl_mem.db.backup_before_qwen_migration"
DEFAULT_EMBEDDING_CACHE = ROOT / "evaluation" / "cache" / "embedding_ablation_v1"
DEFAULT_RERANKER_CACHE = ROOT / "evaluation" / "cache" / "no_answer_calibration_v1" / "reranker.json"
DEFAULT_RESULT = ROOT / "evaluation" / "results" / "no_answer_calibration_v1.json"
THRESHOLDS = tuple(round(value / 100.0, 2) for value in range(30, 81))


def _safe_divide(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def binary_metrics(
    scores: Sequence[float],
    answerable: Sequence[bool],
    threshold: float,
) -> dict[str, Any]:
    """Evaluate ``score >= threshold`` as an answerable-query decision."""
    if len(scores) != len(answerable):
        raise ValueError("scores and answerable labels must have the same length")
    accepted = [float(score) >= float(threshold) for score in scores]
    tp = sum(prediction and truth for prediction, truth in zip(accepted, answerable, strict=True))
    fp = sum(prediction and not truth for prediction, truth in zip(accepted, answerable, strict=True))
    fn = sum(not prediction and truth for prediction, truth in zip(accepted, answerable, strict=True))
    tn = sum(not prediction and not truth for prediction, truth in zip(accepted, answerable, strict=True))
    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    f1 = _safe_divide(2.0 * precision * recall, precision + recall)
    no_answer_precision = _safe_divide(tn, tn + fn)
    no_answer_recall = _safe_divide(tn, tn + fp)
    no_answer_f1 = _safe_divide(
        2.0 * no_answer_precision * no_answer_recall,
        no_answer_precision + no_answer_recall,
    )
    return {
        "threshold": round(float(threshold), 6),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "no_answer_precision": no_answer_precision,
        "no_answer_recall": no_answer_recall,
        "no_answer_f1": no_answer_f1,
        "macro_f1": (f1 + no_answer_f1) / 2.0,
        "balanced_accuracy": (recall + no_answer_recall) / 2.0,
        "answerable_accept_rate": recall,
        "accepted_queries": tp + fp,
        "rejected_queries": tn + fn,
        "total_queries": len(answerable),
        "answerable_queries": tp + fn,
        "no_answer_queries": tn + fp,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def scan_thresholds(
    scores: Sequence[float],
    answerable: Sequence[bool],
    thresholds: Sequence[float] = THRESHOLDS,
) -> list[dict[str, Any]]:
    return [binary_metrics(scores, answerable, threshold) for threshold in thresholds]


def _selection_key(item: dict[str, Any]) -> tuple[float, ...]:
    thresholds = [float(value) for key, value in item.items() if key == "threshold" or key.endswith("_threshold")]
    return (
        float(item["recall"]),
        float(item["macro_f1"]),
        float(item["no_answer_recall"]),
        -sum(thresholds),
    )


def select_operating_point(
    scan: Sequence[dict[str, Any]],
    *,
    min_precision: float = 0.90,
) -> dict[str, Any]:
    """Prefer max answerable recall subject to precision; otherwise macro-F1."""
    constrained = [item for item in scan if float(item["precision"]) >= min_precision]
    if constrained:
        chosen = max(constrained, key=_selection_key)
        rule = f"precision>={min_precision:.2f}_then_max_recall"
    else:
        chosen = max(
            scan,
            key=lambda item: (
                float(item["macro_f1"]),
                float(item["balanced_accuracy"]),
                float(item["f1"]),
                _selection_key(item),
            ),
        )
        rule = "fallback_max_macro_f1"
    return {"selection_rule": rule, "metrics": dict(chosen)}


def scan_and_gate(
    dense_scores: Sequence[float],
    reranker_scores: Sequence[float],
    answerable: Sequence[bool],
    thresholds: Sequence[float] = THRESHOLDS,
) -> list[dict[str, Any]]:
    """Grid-search a transparent AND rule without fitting a statistical model."""
    if not (len(dense_scores) == len(reranker_scores) == len(answerable)):
        raise ValueError("dense, reranker, and labels must have the same length")
    result: list[dict[str, Any]] = []
    for dense_threshold in thresholds:
        for reranker_threshold in thresholds:
            accepted = [
                float(dense) >= dense_threshold and float(reranker) >= reranker_threshold
                for dense, reranker in zip(dense_scores, reranker_scores, strict=True)
            ]
            metrics = binary_metrics([1.0 if value else 0.0 for value in accepted], answerable, 0.5)
            metrics.pop("threshold")
            metrics["dense_threshold"] = round(float(dense_threshold), 6)
            metrics["reranker_threshold"] = round(float(reranker_threshold), 6)
            result.append(metrics)
    return result


def _read_jsonl(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    header: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            for token in stripped[1:].strip().split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    header[key] = value
            continue
        rows.append(json.loads(stripped))
    return header, rows


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database_rows(path: Path, query: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def _fallback_index_text(row: sqlite3.Row) -> str:
    return " ".join(str(part) for part in (row["subject_entity_id"], row["predicate"], row["value_json"]) if part)


def load_frozen_corpus(
    database_path: Path,
    manifest_database_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load Q2 vectors from live DB but membership from a frozen manifest DB."""
    source_path = (
        manifest_database_path if manifest_database_path and manifest_database_path.exists() else database_path
    )
    manifest_rows = _database_rows(source_path, "SELECT id FROM claims WHERE status='active' ORDER BY id")
    manifest_ids = [str(row["id"]) for row in manifest_rows]
    set(manifest_ids)
    live_rows = _database_rows(
        database_path,
        "SELECT id,subject_entity_id,predicate,value_json,status,index_text,"
        "embedding_dense,embedding_model,embedding_dim FROM claims ORDER BY id",
    )
    live_by_id = {str(row["id"]): row for row in live_rows}
    missing_ids = [claim_id for claim_id in manifest_ids if claim_id not in live_by_id]
    if missing_ids:
        raise RuntimeError(f"{len(missing_ids)} manifest claims are absent from the vector database")

    corpus: list[dict[str, Any]] = []
    invalid_vectors: list[dict[str, Any]] = []
    for claim_id in manifest_ids:
        row = live_by_id[claim_id]
        blob = row["embedding_dense"]
        model = row["embedding_model"]
        dim = int(row["embedding_dim"] or 0)
        vector: np.ndarray | None = None
        if blob is not None and model == "qwen3.7-text-embedding" and dim == 2048 and len(blob) == 8192:
            vector = np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)
        else:
            invalid_vectors.append(
                {
                    "claim_id": claim_id,
                    "model": model,
                    "dim": dim,
                    "blob_bytes": len(blob) if blob is not None else None,
                }
            )
        corpus.append(
            {
                "claim_id": claim_id,
                "text": str(row["index_text"] or _fallback_index_text(row)),
                "live_status": str(row["status"]),
                "vector": vector,
            }
        )
    fingerprint = hashlib.sha256("".join(manifest_ids).encode("utf-8")).hexdigest()
    return corpus, {
        "manifest_source": str(source_path.resolve()),
        "vector_source": str(database_path.resolve()),
        "count": len(corpus),
        "fingerprint": fingerprint,
        "non_active_now_but_in_frozen_manifest": [row["claim_id"] for row in corpus if row["live_status"] != "active"],
        "vectors_requiring_api": invalid_vectors,
    }


def _gold_ids(row: dict[str, Any], *, group_aware: bool) -> set[str]:
    if not group_aware:
        return {str(value) for value in row.get("gold_ids", [])}
    groups = row.get("gold_groups") or []
    values = {str(value) for group in groups for value in group}
    return values or {str(value) for value in row.get("gold_ids", [])}


def validate_gold_membership(rows: list[dict[str, Any]], corpus_ids: set[str]) -> dict[str, Any]:
    strict_missing: list[str] = []
    group_missing: list[str] = []
    for row in rows:
        if bool(row["no_answer"]):
            continue
        if not (_gold_ids(row, group_aware=False) & corpus_ids):
            strict_missing.append(str(row["id"]))
        if not (_gold_ids(row, group_aware=True) & corpus_ids):
            group_missing.append(str(row["id"]))
    return {
        "strict_gold_missing_query_ids": strict_missing,
        "group_aware_gold_missing_query_ids": group_missing,
        "labels_usable": not group_missing,
    }


def _load_toml_reranker(path: Path) -> tuple[str, str]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    section = data.get("reranker") or {}
    return (
        str(section.get("base_url") or "https://dashscope.aliyuncs.com"),
        str(section.get("model") or "gte-rerank-v2"),
    )


def _load_reranker_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entries": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("entries"), dict):
        return {"entries": {}}
    return payload


def _save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _reranker_key(model: str, query: str, candidate_ids: Sequence[str], documents: Sequence[str]) -> str:
    canonical = json.dumps(
        {"model": model, "query": query, "candidate_ids": candidate_ids, "documents": documents},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rerank_queries(
    rows: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    dense_scores: np.ndarray,
    *,
    api_key: str,
    base_url: str,
    model: str,
    cache_path: Path,
    candidate_limit: int,
) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    client = DashScopeReranker(api_key, base_url, model, timeout=60.0)
    cache = _load_reranker_cache(cache_path)
    entries: dict[str, Any] = cache["entries"]
    scores: list[float] = []
    details: list[dict[str, Any]] = []
    network_calls = 0
    cache_hits = 0
    latency = 0.0
    for index, row in enumerate(rows):
        order = np.argsort(-dense_scores[index], kind="stable")[:candidate_limit]
        candidate_ids = [str(corpus[item]["claim_id"]) for item in order]
        documents = [str(corpus[item]["text"]) for item in order]
        key = _reranker_key(model, str(row["query"]), candidate_ids, documents)
        cached = entries.get(key)
        if cached is None:
            started = time.perf_counter()
            ranked = client.rerank(str(row["query"]), documents, top_n=len(documents))
            latency += time.perf_counter() - started
            network_calls += 1
            if not ranked:
                raise RuntimeError(f"reranker returned no results for {row['id']}")
            best_index, best_score = ranked[0]
            cached = {
                "query_id": row["id"],
                "score": float(best_score),
                "claim_id": candidate_ids[best_index],
                "candidate_count": len(documents),
            }
            entries[key] = cached
            cache.update({"model": model, "entries": entries})
            _save_json_atomic(cache_path, cache)
        else:
            cache_hits += 1
        scores.append(float(cached["score"]))
        details.append(
            {
                "score": float(cached["score"]),
                "claim_id": str(cached["claim_id"]),
                "candidate_count": int(cached["candidate_count"]),
            }
        )
        print(
            f"[reranker] {index + 1}/{len(rows)} network={network_calls} cache_hits={cache_hits}",
            file=sys.stderr,
            flush=True,
        )
    return (
        np.asarray(scores, dtype=np.float64),
        details,
        {
            "model": model,
            "candidate_pool": f"dense_top_{candidate_limit}",
            "network_calls_this_run": network_calls,
            "cache_hits": cache_hits,
            "latency_seconds_this_run": latency,
            "cache_path": str(cache_path.resolve()),
        },
    )


def _split_values(
    rows: list[dict[str, Any]],
    scores: Sequence[float],
    split: str,
) -> tuple[list[float], list[bool], list[int]]:
    indexes = [index for index, row in enumerate(rows) if row["split"] == split]
    return (
        [float(scores[index]) for index in indexes],
        [not bool(rows[index]["no_answer"]) for index in indexes],
        indexes,
    )


def _evaluate_feature(
    rows: list[dict[str, Any]],
    scores: Sequence[float],
    *,
    min_precision: float,
) -> dict[str, Any]:
    dev_scores, dev_labels, dev_indexes = _split_values(rows, scores, "dev")
    test_scores, test_labels, test_indexes = _split_values(rows, scores, "test")
    scan = scan_thresholds(dev_scores, dev_labels)
    selected = select_operating_point(scan, min_precision=min_precision)
    threshold = float(selected["metrics"]["threshold"])
    test_metrics = binary_metrics(test_scores, test_labels, threshold)
    best_answer_f1 = max(scan, key=lambda item: (item["f1"], item["recall"], -item["threshold"]))
    best_macro_f1 = max(scan, key=lambda item: (item["macro_f1"], item["balanced_accuracy"], -item["threshold"]))
    return {
        "dev_best_threshold": threshold,
        "threshold_selection": selected["selection_rule"],
        "dev_metrics_at_threshold": selected["metrics"],
        "test_metrics_at_threshold": test_metrics,
        "dev_best_answerable_f1": best_answer_f1,
        "test_metrics_at_dev_best_answerable_f1": binary_metrics(
            test_scores,
            test_labels,
            float(best_answer_f1["threshold"]),
        ),
        "dev_best_macro_f1": best_macro_f1,
        "test_metrics_at_dev_best_macro_f1": binary_metrics(
            test_scores,
            test_labels,
            float(best_macro_f1["threshold"]),
        ),
        "dev_predicted_no_answer_ids": [
            str(rows[index]["id"]) for index in dev_indexes if float(scores[index]) < threshold
        ],
        "test_predicted_no_answer_ids": [
            str(rows[index]["id"]) for index in test_indexes if float(scores[index]) < threshold
        ],
        "threshold_scan": scan,
    }


def _score_distribution(
    rows: list[dict[str, Any]],
    scores: Sequence[float],
) -> dict[str, dict[str, dict[str, float | int]]]:
    result: dict[str, dict[str, dict[str, float | int]]] = {}
    for split in ("dev", "test"):
        result[split] = {}
        for label, no_answer in (("answerable", False), ("no_answer", True)):
            values = np.asarray(
                [
                    float(scores[index])
                    for index, row in enumerate(rows)
                    if row["split"] == split and bool(row["no_answer"]) is no_answer
                ],
                dtype=np.float64,
            )
            result[split][label] = {
                "count": int(len(values)),
                "min": float(np.min(values)),
                "p25": float(np.quantile(values, 0.25)),
                "median": float(np.median(values)),
                "p75": float(np.quantile(values, 0.75)),
                "max": float(np.max(values)),
            }
    return result


def _evaluate_and_gate(
    rows: list[dict[str, Any]],
    dense_scores: Sequence[float],
    reranker_scores: Sequence[float],
    *,
    min_precision: float,
) -> dict[str, Any]:
    dev_dense, dev_labels, dev_indexes = _split_values(rows, dense_scores, "dev")
    dev_reranker, _, _ = _split_values(rows, reranker_scores, "dev")
    scan = scan_and_gate(dev_dense, dev_reranker, dev_labels)
    selected = select_operating_point(scan, min_precision=min_precision)
    metrics = selected["metrics"]
    dense_threshold = float(metrics["dense_threshold"])
    reranker_threshold = float(metrics["reranker_threshold"])
    test_indexes = [index for index, row in enumerate(rows) if row["split"] == "test"]
    test_labels = [not bool(rows[index]["no_answer"]) for index in test_indexes]
    test_accepted = [
        float(dense_scores[index]) >= dense_threshold and float(reranker_scores[index]) >= reranker_threshold
        for index in test_indexes
    ]
    test_metrics = binary_metrics([1.0 if item else 0.0 for item in test_accepted], test_labels, 0.5)
    test_metrics.pop("threshold")
    test_metrics["dense_threshold"] = dense_threshold
    test_metrics["reranker_threshold"] = reranker_threshold
    ranked_scan = sorted(
        scan,
        key=lambda item: (
            item["precision"] >= min_precision,
            item["recall"],
            item["macro_f1"],
            item["no_answer_recall"],
        ),
        reverse=True,
    )
    return {
        "dev_best_threshold": {"dense": dense_threshold, "reranker": reranker_threshold},
        "threshold_selection": selected["selection_rule"],
        "dev_metrics_at_threshold": metrics,
        "test_metrics_at_threshold": test_metrics,
        "dev_predicted_no_answer_ids": [
            str(rows[index]["id"])
            for index in dev_indexes
            if not (
                float(dense_scores[index]) >= dense_threshold and float(reranker_scores[index]) >= reranker_threshold
            )
        ],
        "test_predicted_no_answer_ids": [
            str(rows[index]["id"])
            for index in test_indexes
            if not (
                float(dense_scores[index]) >= dense_threshold and float(reranker_scores[index]) >= reranker_threshold
            )
        ],
        "top_dev_grid_points": ranked_scan[:20],
        "grid_points_evaluated": len(scan),
    }


def _recommendation(
    dense: dict[str, Any], reranker: dict[str, Any] | None, fused: dict[str, Any] | None
) -> dict[str, Any]:
    candidates = [("dense_cosine_top1", dense)]
    if reranker is not None:
        candidates.append(("reranker_top1", reranker))
    if fused is not None:
        candidates.append(("dense_and_reranker", fused))
    best_name, best = max(
        candidates,
        key=lambda item: (
            item[1]["dev_metrics_at_threshold"]["macro_f1"],
            item[1]["dev_metrics_at_threshold"]["balanced_accuracy"],
        ),
    )
    test = best["test_metrics_at_threshold"]
    enforce_ready = (
        test["precision"] >= 0.90
        and test["recall"] >= 0.80
        and test["no_answer_precision"] >= 0.80
        and test["no_answer_recall"] >= 0.70
    )
    dense_threshold = dense["dev_best_threshold"]
    if enforce_ready:
        summary = f"test 达到保守上线门槛；可先以 {best_name} 的 dev 阈值小流量 enforce，" "并持续观察误拒率。"
        mode = "staged_enforce"
    else:
        summary = (
            f"test 未达到拒答上线门槛；生产保持 observe。dense 阈值 {dense_threshold:.2f} "
            "只作为影子诊断，不应直接截断结果；需要验证真实多通道分数或训练更强的 answerability 特征。"
        )
        mode = "observe"
    return {
        "mode": mode,
        "best_dev_feature": best_name,
        "dense_diagnostic_threshold": dense_threshold,
        "enforce_readiness_criteria": {
            "test_precision": 0.90,
            "test_recall": 0.80,
            "test_no_answer_precision": 0.80,
            "test_no_answer_recall": 0.70,
        },
        "criteria_met": enforce_ready,
        "summary": summary,
        "production_integration_warning": (
            "当前 relevance_keep_top1=true 时即使切到 enforce 也会保留第一条候选；"
            "而本实验的 dense-top1 阈值没有覆盖 FTS/RRF 后的真实候选路径。"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--corpus-manifest-db", type=Path, default=DEFAULT_MANIFEST_DATABASE)
    parser.add_argument("--embedding-cache", type=Path, default=DEFAULT_EMBEDDING_CACHE)
    parser.add_argument("--reranker-cache", type=Path, default=DEFAULT_RERANKER_CACHE)
    parser.add_argument("--result", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--config", type=Path, default=ROOT / "hl_mem.toml")
    parser.add_argument("--min-precision", type=float, default=0.90)
    parser.add_argument("--reranker-candidate-limit", type=int, default=50)
    parser.add_argument("--skip-reranker", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    header, rows = _read_jsonl(args.dataset)
    if len(rows) != 80:
        raise RuntimeError(f"expected 80 recall rows, found {len(rows)}")
    split_counts = {
        split: {
            "total": sum(row["split"] == split for row in rows),
            "answerable": sum(row["split"] == split and not row["no_answer"] for row in rows),
            "no_answer": sum(row["split"] == split and row["no_answer"] for row in rows),
        }
        for split in ("dev", "test")
    }
    corpus, corpus_metadata = load_frozen_corpus(args.database, args.corpus_manifest_db)
    corpus_ids = {str(row["claim_id"]) for row in corpus}
    gold_validation = validate_gold_membership(rows, corpus_ids)
    if not gold_validation["labels_usable"]:
        raise RuntimeError("one or more answerable queries have no group-aware gold in the frozen corpus")

    api_key = _load_env_value(args.env_file, "EMBEDDING_API_KEY")
    if not api_key:
        raise RuntimeError("EMBEDDING_API_KEY is required when a Q2 cache batch or corpus vector is missing")
    config = CONFIGS["Q2"]
    embedding_client = DashScopeEmbeddingClient(api_key)
    try:
        query_output = embed_remote(
            embedding_client,
            config,
            "query",
            [str(row["query"]) for row in rows],
            cache_dir=args.embedding_cache,
            use_cache=True,
        )
        missing_indexes = [index for index, row in enumerate(corpus) if row["vector"] is None]
        document_cost = None
        if missing_indexes:
            missing_output = embed_remote(
                embedding_client,
                config,
                "document",
                [str(corpus[index]["text"]) for index in missing_indexes],
                cache_dir=args.embedding_cache,
                use_cache=True,
            )
            for local_index, corpus_index in enumerate(missing_indexes):
                corpus[corpus_index]["vector"] = missing_output.dense[local_index]
            document_cost = missing_output.cost.as_dict()
    finally:
        embedding_client.close()

    document_matrix = np.stack([np.asarray(row["vector"], dtype=np.float32) for row in corpus])
    query_matrix = np.asarray(query_output.dense, dtype=np.float32)
    document_normalized = _normalize_rows(document_matrix)
    query_normalized = _normalize_rows(query_matrix)
    all_dense_scores = query_normalized @ document_normalized.T
    top_indexes = np.argmax(all_dense_scores, axis=1)
    top_scores = all_dense_scores[np.arange(len(rows)), top_indexes].astype(np.float64)

    dense_analysis = _evaluate_feature(rows, top_scores, min_precision=args.min_precision)
    reranker_analysis: dict[str, Any] | None = None
    fused_analysis: dict[str, Any] | None = None
    reranker_details: list[dict[str, Any] | None] = [None] * len(rows)
    reranker_metadata: dict[str, Any] = {"status": "skipped"}
    reranker_scores: np.ndarray | None = None
    if not args.skip_reranker:
        reranker_key = _load_env_value(args.env_file, "RERANKER_API_KEY")
        if not reranker_key:
            reranker_metadata = {"status": "unavailable", "reason": "RERANKER_API_KEY missing"}
        else:
            base_url, model = _load_toml_reranker(args.config)
            reranker_scores, reranker_details_raw, reranker_metadata = rerank_queries(
                rows,
                corpus,
                all_dense_scores,
                api_key=reranker_key,
                base_url=base_url,
                model=model,
                cache_path=args.reranker_cache,
                candidate_limit=args.reranker_candidate_limit,
            )
            reranker_details = list(reranker_details_raw)
            reranker_metadata["status"] = "completed"
            reranker_analysis = _evaluate_feature(rows, reranker_scores, min_precision=args.min_precision)
            fused_analysis = _evaluate_and_gate(
                rows,
                top_scores,
                reranker_scores,
                min_precision=args.min_precision,
            )

    claim_text = {str(row["claim_id"]): str(row["text"]) for row in corpus}
    per_query: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        dense_claim_id = str(corpus[int(top_indexes[index])]["claim_id"])
        detail = {
            "id": row["id"],
            "query": row["query"],
            "split": row["split"],
            "no_answer": bool(row["no_answer"]),
            "dense_top1_score": float(top_scores[index]),
            "dense_top1_claim_id": dense_claim_id,
            "dense_top1_text": claim_text[dense_claim_id],
        }
        if reranker_scores is not None and reranker_details[index] is not None:
            reranker_detail = reranker_details[index]
            reranker_claim_id = str(reranker_detail["claim_id"])
            detail.update(
                {
                    "reranker_top1_score": float(reranker_scores[index]),
                    "reranker_top1_claim_id": reranker_claim_id,
                    "reranker_top1_text": claim_text[reranker_claim_id],
                }
            )
        per_query.append(detail)

    recommendation = _recommendation(dense_analysis, reranker_analysis, fused_analysis)
    result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": "Q2",
        "feature": "dense_cosine_top1",
        "label_semantics": {
            "positive": "answerable",
            "accepted": "score >= threshold",
            "no_answer_precision": "among rejected queries, fraction truly no-answer",
            "no_answer_recall": "among true no-answer queries, fraction rejected",
        },
        "dataset": {
            "path": str(args.dataset.resolve()),
            "sha256": _file_sha256(args.dataset),
            "header": header,
            "split_counts": split_counts,
            "gold_membership": gold_validation,
        },
        "corpus": corpus_metadata,
        "embedding": {
            "model": config.model,
            "api": "native",
            "dimension": config.dim,
            "document_text_type": "document",
            "query_text_type": "query",
            "query_cost": query_output.cost.as_dict(),
            "missing_document_cost": document_cost,
            "cache_path": str(args.embedding_cache.resolve()),
        },
        "dev_best_threshold": dense_analysis["dev_best_threshold"],
        "dev_metrics_at_threshold": dense_analysis["dev_metrics_at_threshold"],
        "test_metrics_at_threshold": dense_analysis["test_metrics_at_threshold"],
        "threshold_scan": dense_analysis["threshold_scan"],
        "dense_cosine_top1": dense_analysis,
        "dense_score_distribution": _score_distribution(rows, top_scores),
        "reranker": reranker_metadata,
        "reranker_top1": reranker_analysis,
        "reranker_score_distribution": (
            _score_distribution(rows, reranker_scores) if reranker_scores is not None else None
        ),
        "dense_and_reranker": fused_analysis,
        "per_query": per_query,
        "recommendation": recommendation,
    }
    _save_json_atomic(args.result, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
