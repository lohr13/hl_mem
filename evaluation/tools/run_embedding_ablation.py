"""Run the frozen V0/Q0-Q4 embedding ablation without starting hl_mem.

The script intentionally imports neither ``hl_mem.application`` nor any service
component. Progress goes to stderr; the final benchmark payload goes to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://dashscope.aliyuncs.com"
COMPATIBLE_PATH = "/compatible-mode/v1/embeddings"
NATIVE_PATH = "/api/v1/services/embeddings/text-embedding/text-embedding"
QUERY_INSTRUCT = "Represent the sentence for retrieving relevant memory facts"
PAIR_DATASET = ROOT / "evaluation" / "datasets" / "claim_pair_eval_v1.jsonl"
RECALL_DATASET = ROOT / "evaluation" / "datasets" / "recall_eval_v1.jsonl"
DEFAULT_DATABASE = ROOT / "var" / "hl_mem.db"
DEFAULT_RESULT = ROOT / "evaluation" / "results" / "embedding_ablation_v1.json"
DEFAULT_SMOKE_RESULT = ROOT / "evaluation" / "results" / "embedding_ablation_v1_smoke.json"
DEFAULT_CACHE = ROOT / "evaluation" / "cache" / "embedding_ablation_v1"


@dataclass(frozen=True)
class EmbeddingConfig:
    code: str
    model: str
    api_kind: str
    dim: int = 2048
    batch_size: int = 10
    use_text_type: bool = False
    use_instruct: bool = False
    use_sparse: bool = False


CONFIGS: dict[str, EmbeddingConfig] = {
    "V0": EmbeddingConfig("V0", "text-embedding-v4", "compatible", batch_size=10),
    "Q0": EmbeddingConfig("Q0", "qwen3.7-text-embedding", "compatible", batch_size=20),
    "Q1": EmbeddingConfig("Q1", "qwen3.7-text-embedding", "native", batch_size=10),
    "Q2": EmbeddingConfig("Q2", "qwen3.7-text-embedding", "native", batch_size=10, use_text_type=True),
    "Q3": EmbeddingConfig(
        "Q3",
        "qwen3.7-text-embedding",
        "native",
        batch_size=10,
        use_text_type=True,
        use_instruct=True,
    ),
    "Q4": EmbeddingConfig(
        "Q4",
        "qwen3.7-text-embedding",
        "native",
        batch_size=10,
        use_text_type=True,
        use_instruct=True,
        use_sparse=True,
    ),
}


@dataclass
class Cost:
    api_calls: int = 0
    tokens: int = 0
    latency_seconds: float = 0.0
    network_api_calls_this_run: int = 0
    cache_hit_batches: int = 0
    db_cached_vectors: int = 0

    def add(self, other: "Cost") -> None:
        self.api_calls += other.api_calls
        self.tokens += other.tokens
        self.latency_seconds += other.latency_seconds
        self.network_api_calls_this_run += other.network_api_calls_this_run
        self.cache_hit_batches += other.cache_hit_batches
        self.db_cached_vectors += other.db_cached_vectors

    def as_dict(self) -> dict[str, int | float]:
        return {
            "api_calls": self.api_calls,
            "tokens": self.tokens,
            "latency_seconds": round(self.latency_seconds, 6),
            "network_api_calls_this_run": self.network_api_calls_this_run,
            "cache_hit_batches": self.cache_hit_batches,
            "db_cached_vectors": self.db_cached_vectors,
        }


@dataclass
class EmbeddingOutput:
    dense: np.ndarray
    sparse: list[dict[int, float]] | None
    cost: Cost = field(default_factory=Cost)


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    index_text: str
    status: str
    v0_dense: np.ndarray | None


@dataclass
class DatabaseCorpus:
    active: list[ClaimRecord]
    text_by_id: dict[str, str]
    v0_by_text: dict[str, np.ndarray]
    fingerprint: str


def build_request(config: EmbeddingConfig, role: str, texts: list[str]) -> tuple[str, dict[str, Any]]:
    if role not in {"document", "query"}:
        raise ValueError(f"unsupported embedding role: {role}")
    if config.api_kind == "compatible":
        return COMPATIBLE_PATH, {"model": config.model, "input": texts, "dimensions": config.dim}

    parameters: dict[str, Any] = {"dimension": config.dim}
    if config.use_text_type:
        parameters["text_type"] = role
    # Q2 is the text_type-only ablation. Q3/Q4 add query-side instruct.
    if config.use_instruct and role == "query":
        parameters["instruct"] = QUERY_INSTRUCT
    if config.use_sparse:
        parameters["output_type"] = "dense&sparse"
    return NATIVE_PATH, {
        "model": config.model,
        "input": {"texts": texts},
        "parameters": parameters,
    }


def _parse_sparse(value: Any) -> dict[int, float]:
    if value is None:
        return {}
    if isinstance(value, list):
        result: dict[int, float] = {}
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("sparse_embedding list entries must be objects")
            index = item.get("index", item.get("token_id"))
            weight = item.get("value", item.get("weight"))
            if index is None or weight is None:
                raise ValueError("sparse_embedding entry lacks index/value")
            result[int(index)] = float(weight)
        return result
    if isinstance(value, dict):
        if "indices" in value and "values" in value:
            indices = value["indices"]
            values = value["values"]
            if len(indices) != len(values):
                raise ValueError("sparse indices/values length mismatch")
            return {int(index): float(weight) for index, weight in zip(indices, values, strict=True)}
        return {int(index): float(weight) for index, weight in value.items()}
    raise ValueError(f"unsupported sparse embedding shape: {type(value).__name__}")


def parse_api_response(
    config: EmbeddingConfig,
    payload: dict[str, Any],
    *,
    expected_count: int,
    expected_dim: int | None = None,
) -> tuple[np.ndarray, list[dict[int, float]] | None, int]:
    dim = config.dim if expected_dim is None else expected_dim
    if config.api_kind == "compatible":
        items = sorted(payload.get("data", []), key=lambda item: int(item.get("index", 0)))
    else:
        output = payload.get("output") or {}
        items = sorted(
            output.get("embeddings", []),
            key=lambda item: int(item.get("text_index", item.get("index", 0))),
        )
    if len(items) != expected_count:
        raise ValueError(f"embedding response count mismatch: expected {expected_count}, got {len(items)}")

    dense = np.asarray([item.get("embedding", item.get("dense_embedding")) for item in items], dtype=np.float32)
    if dense.shape != (expected_count, dim):
        raise ValueError(f"embedding response shape mismatch: expected {(expected_count, dim)}, got {dense.shape}")

    sparse: list[dict[int, float]] | None = None
    if config.use_sparse:
        sparse = []
        for item in items:
            raw_sparse = item.get("sparse_embedding", item.get("sparse_embeddings"))
            if raw_sparse is None:
                raise ValueError("Q4 response did not include sparse_embedding")
            sparse.append(_parse_sparse(raw_sparse))

    usage = payload.get("usage") or {}
    tokens = int(usage.get("total_tokens", usage.get("input_tokens", usage.get("prompt_tokens", 0))) or 0)
    return dense, sparse, tokens


def _safe_ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def average_precision(labels: np.ndarray, scores: np.ndarray) -> float:
    labels = np.asarray(labels, dtype=np.int8)
    scores = np.asarray(scores, dtype=np.float64)
    positives = int(labels.sum())
    if positives == 0:
        return 0.0
    order = np.argsort(-scores, kind="stable")
    ranked = labels[order]
    cumulative = np.cumsum(ranked)
    precision_at_rank = cumulative / np.arange(1, len(ranked) + 1)
    return float(np.sum(precision_at_rank * ranked) / positives)


def classification_metrics(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, Any]:
    labels = np.asarray(labels, dtype=np.int8)
    predictions = np.asarray(scores, dtype=np.float64) >= threshold
    positives = labels == 1
    negatives = ~positives
    tp = int(np.sum(predictions & positives))
    fp = int(np.sum(predictions & negatives))
    fn = int(np.sum(~predictions & positives))
    tn = int(np.sum(~predictions & negatives))
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _safe_ratio(2.0 * precision * recall, precision + recall)
    return {
        "threshold": float(threshold),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_merge_rate": _safe_ratio(fp, fp + tn),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def _binary_threshold_metrics(
    labels: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    *,
    positive_when_low: bool,
) -> dict[str, Any]:
    labels = np.asarray(labels, dtype=np.int8)
    values = np.asarray(scores, dtype=np.float64)
    predictions = values < threshold if positive_when_low else values >= threshold
    positives = labels == 1
    tp = int(np.sum(predictions & positives))
    fp = int(np.sum(predictions & ~positives))
    fn = int(np.sum(~predictions & positives))
    tn = int(np.sum(~predictions & ~positives))
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = _safe_ratio(2.0 * precision * recall, precision + recall)
    return {
        "threshold": float(threshold),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": _safe_ratio(tp + tn, len(labels)),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def select_threshold(
    labels: np.ndarray,
    scores: np.ndarray,
    *,
    positive_when_low: bool = False,
) -> dict[str, Any]:
    labels = np.asarray(labels, dtype=np.int8)
    values = np.asarray(scores, dtype=np.float64)
    if len(labels) == 0 or len(labels) != len(values):
        raise ValueError("threshold calibration needs aligned non-empty labels and scores")
    unique = np.unique(values)
    epsilon = max(1e-12, float(np.ptp(unique)) * 1e-9)
    candidates = [float(unique[0] - epsilon), float(unique[-1] + epsilon)]
    candidates.extend(float((left + right) / 2.0) for left, right in zip(unique[:-1], unique[1:], strict=True))
    candidates.extend(float(value) for value in unique)

    evaluated = [
        _binary_threshold_metrics(labels, values, threshold, positive_when_low=positive_when_low)
        for threshold in sorted(set(candidates))
    ]
    return max(
        evaluated,
        key=lambda item: (
            item["f1"],
            item["accuracy"],
            item["precision"],
            -abs(item["threshold"] - float(np.median(values))),
        ),
    )


def _ranks_descending(scores: np.ndarray) -> np.ndarray:
    order = np.argsort(-np.asarray(scores), kind="stable")
    ranks = np.empty(len(order), dtype=np.int64)
    ranks[order] = np.arange(1, len(order) + 1)
    return ranks


def rrf_fuse(
    dense_scores: np.ndarray,
    sparse_scores: np.ndarray,
    *,
    dense_weight: float = 0.6,
    rank_constant: int = 60,
) -> np.ndarray:
    dense_ranks = _ranks_descending(dense_scores)
    sparse_ranks = _ranks_descending(sparse_scores)
    return dense_weight / (rank_constant + dense_ranks) + (1.0 - dense_weight) / (rank_constant + sparse_ranks)


def recall_metrics(
    rows: list[dict[str, Any]],
    rankings: dict[str, list[str]],
    top1_scores: dict[str, float],
    *,
    no_answer_threshold: float,
    top_k: int = 5,
) -> dict[str, Any]:
    answerable = [row for row in rows if not row["no_answer"]]
    no_answer = [row for row in rows if row["no_answer"]]

    strict_hits = 0
    strict_rr = 0.0
    group_hits = 0
    group_rr = 0.0
    for row in answerable:
        ranking = rankings[row["id"]]
        strict_gold = set(row["gold_ids"])
        group_gold = {claim_id for group in row.get("gold_groups", []) for claim_id in group}
        strict_rank = next((index for index, claim_id in enumerate(ranking, 1) if claim_id in strict_gold), None)
        group_rank = next((index for index, claim_id in enumerate(ranking, 1) if claim_id in group_gold), None)
        if strict_rank is not None:
            strict_rr += 1.0 / strict_rank
            strict_hits += int(strict_rank <= top_k)
        if group_rank is not None:
            group_rr += 1.0 / group_rank
            group_hits += int(group_rank <= top_k)

    rejected_no_answer = sum(top1_scores[row["id"]] < no_answer_threshold for row in no_answer)
    predicted_no_answer = [row for row in rows if top1_scores[row["id"]] < no_answer_threshold]
    correctly_predicted_no_answer = sum(bool(row["no_answer"]) for row in predicted_no_answer)
    accepted_answerable = sum(top1_scores[row["id"]] >= no_answer_threshold for row in answerable)
    no_answer_recall = _safe_ratio(rejected_no_answer, len(no_answer))
    predicted_no_answer_precision = _safe_ratio(correctly_predicted_no_answer, len(predicted_no_answer))
    no_answer_f1 = _safe_ratio(
        2.0 * no_answer_recall * predicted_no_answer_precision,
        no_answer_recall + predicted_no_answer_precision,
    )
    answerable_accept_rate = _safe_ratio(accepted_answerable, len(answerable))

    return {
        "hit_at_5": _safe_ratio(strict_hits, len(answerable)),
        "mrr": _safe_ratio(strict_rr, len(answerable)),
        "group_aware_hit_at_5": _safe_ratio(group_hits, len(answerable)),
        "group_aware_mrr": _safe_ratio(group_rr, len(answerable)),
        # Kept under the requested name: fraction of true no-answer queries rejected.
        "no_answer_precision": no_answer_recall,
        "predicted_no_answer_precision": predicted_no_answer_precision,
        "no_answer_f1": no_answer_f1,
        "answerable_accept_rate": answerable_accept_rate,
        "answerability_balanced_accuracy": (no_answer_recall + answerable_accept_rate) / 2.0,
        "degenerate_gate": not predicted_no_answer or len(predicted_no_answer) == len(rows),
        "no_answer_threshold": float(no_answer_threshold),
        "answerable_queries": len(answerable),
        "no_answer_queries": len(no_answer),
        "predicted_no_answer_queries": len(predicted_no_answer),
    }


def _read_jsonl(path: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    header: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decode_v0_blob(blob: bytes | None, model: Any, dim: Any) -> np.ndarray | None:
    if blob is None or model != "text-embedding-v4" or int(dim or 0) != 2048 or len(blob) != 8192:
        return None
    return np.frombuffer(blob, dtype="<f4").astype(np.float32, copy=True)


def _fallback_index_text(row: sqlite3.Row) -> str:
    return " ".join(part for part in (row["subject_entity_id"], row["predicate"], row["value_json"]) if part)


def load_database(path: Path) -> DatabaseCorpus:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id,subject_entity_id,predicate,value_json,status,index_text,"
            "embedding_dense,embedding_model,embedding_dim FROM claims ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    active: list[ClaimRecord] = []
    text_by_id: dict[str, str] = {}
    v0_by_text: dict[str, np.ndarray] = {}
    for row in rows:
        text = str(row["index_text"] or _fallback_index_text(row))
        claim_id = str(row["id"])
        vector = _decode_v0_blob(row["embedding_dense"], row["embedding_model"], row["embedding_dim"])
        text_by_id[claim_id] = text
        if vector is not None:
            v0_by_text.setdefault(text, vector)
        if row["status"] == "active":
            active.append(ClaimRecord(claim_id, text, "active", vector))
    fingerprint = hashlib.sha256("".join(record.claim_id for record in active).encode("utf-8")).hexdigest()
    return DatabaseCorpus(active, text_by_id, v0_by_text, fingerprint)


def _load_env_value(path: Path, key: str) -> str | None:
    existing = os.getenv(key)
    if existing:
        return existing
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value or None
    return None


class DashScopeEmbeddingClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout_seconds: float = 90.0,
        max_attempts: int = 3,
        trust_env: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            trust_env=trust_env,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        config: EmbeddingConfig,
        role: str,
        texts: list[str],
    ) -> EmbeddingOutput:
        path, body = build_request(config, role, texts)
        cost = Cost()
        response: httpx.Response | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.perf_counter()
            cost.api_calls += 1
            cost.network_api_calls_this_run += 1
            try:
                response = self.client.post(f"{self.base_url}{path}", json=body)
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as error:
                status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                retryable = status is None or status == 429 or status >= 500
                if attempt >= self.max_attempts or not retryable:
                    detail = ""
                    if isinstance(error, httpx.HTTPStatusError):
                        detail = f" response={error.response.text[:500]!r}"
                    raise RuntimeError(
                        f"{config.code} {role} embedding failed after {attempt} attempt(s): "
                        f"{type(error).__name__} status={status}.{detail}"
                    ) from error
                time.sleep(0.5 * (2 ** (attempt - 1)))
            finally:
                cost.latency_seconds += time.perf_counter() - started
        if response is None:
            raise RuntimeError("embedding request did not produce a response")
        dense, sparse, tokens = parse_api_response(config, response.json(), expected_count=len(texts))
        cost.tokens += tokens
        return EmbeddingOutput(dense, sparse, cost)


def _sparse_to_arrays(sparse: list[dict[int, float]] | None, count: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    indptr = [0]
    indices: list[int] = []
    values: list[float] = []
    for row in sparse or [{} for _ in range(count)]:
        for index, value in sorted(row.items()):
            indices.append(int(index))
            values.append(float(value))
        indptr.append(len(indices))
    return (
        np.asarray(indptr, dtype=np.int64),
        np.asarray(indices, dtype=np.int64),
        np.asarray(values, dtype=np.float32),
    )


def _arrays_to_sparse(indptr: np.ndarray, indices: np.ndarray, values: np.ndarray) -> list[dict[int, float]]:
    result: list[dict[int, float]] = []
    for row_index in range(len(indptr) - 1):
        start, end = int(indptr[row_index]), int(indptr[row_index + 1])
        result.append(
            {int(index): float(value) for index, value in zip(indices[start:end], values[start:end], strict=True)}
        )
    return result


def _cache_key(config: EmbeddingConfig, role: str, texts: list[str]) -> str:
    value = {
        "config": config.code,
        "model": config.model,
        "api_kind": config.api_kind,
        "dim": config.dim,
        "role": role,
        "use_text_type": config.use_text_type,
        "use_instruct": config.use_instruct,
        "use_sparse": config.use_sparse,
        "texts": texts,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_cached_batch(
    config: EmbeddingConfig,
    role: str,
    texts: list[str],
    cache_dir: Path,
) -> EmbeddingOutput | None:
    key = _cache_key(config, role, texts)
    data_path = cache_dir / f"{config.code}-{role}-{key[:24]}.npz"
    meta_path = data_path.with_suffix(".json")
    if not data_path.exists() or not meta_path.exists():
        return None
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if metadata.get("cache_key") != key or metadata.get("text_count") != len(texts):
        return None
    with np.load(data_path, allow_pickle=False) as stored:
        dense = stored["dense"].astype(np.float32, copy=True)
        if dense.shape != (len(texts), config.dim):
            return None
        sparse = None
        if bool(metadata.get("has_sparse")):
            sparse = _arrays_to_sparse(stored["sparse_indptr"], stored["sparse_indices"], stored["sparse_values"])
    cached_cost = metadata.get("cost") or {}
    cost = Cost(
        api_calls=int(cached_cost.get("api_calls", 0)),
        tokens=int(cached_cost.get("tokens", 0)),
        latency_seconds=float(cached_cost.get("latency_seconds", 0.0)),
        network_api_calls_this_run=0,
        cache_hit_batches=1,
    )
    return EmbeddingOutput(dense, sparse, cost)


def _save_cached_batch(
    config: EmbeddingConfig,
    role: str,
    texts: list[str],
    output: EmbeddingOutput,
    cache_dir: Path,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(config, role, texts)
    data_path = cache_dir / f"{config.code}-{role}-{key[:24]}.npz"
    meta_path = data_path.with_suffix(".json")
    indptr, indices, values = _sparse_to_arrays(output.sparse, len(texts))
    data_temp = data_path.with_suffix(".npz.tmp")
    meta_temp = meta_path.with_suffix(".json.tmp")
    with data_temp.open("wb") as handle:
        np.savez_compressed(
            handle,
            dense=output.dense.astype(np.float32),
            sparse_indptr=indptr,
            sparse_indices=indices,
            sparse_values=values,
        )
    metadata = {
        "cache_key": key,
        "config": config.code,
        "role": role,
        "text_count": len(texts),
        "has_sparse": output.sparse is not None,
        "cost": output.cost.as_dict(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    data_temp.replace(data_path)
    meta_temp.replace(meta_path)


def embed_remote(
    client: DashScopeEmbeddingClient,
    config: EmbeddingConfig,
    role: str,
    texts: list[str],
    *,
    cache_dir: Path,
    use_cache: bool,
) -> EmbeddingOutput:
    if not texts:
        return EmbeddingOutput(np.empty((0, config.dim), dtype=np.float32), [] if config.use_sparse else None)
    dense_batches: list[np.ndarray] = []
    sparse_rows: list[dict[int, float]] | None = [] if config.use_sparse else None
    total_cost = Cost()
    batch_count = (len(texts) + config.batch_size - 1) // config.batch_size
    for batch_index, start in enumerate(range(0, len(texts), config.batch_size), 1):
        batch = texts[start : start + config.batch_size]
        output = _load_cached_batch(config, role, batch, cache_dir) if use_cache else None
        if output is None:
            output = client.request(config, role, batch)
            if use_cache:
                _save_cached_batch(config, role, batch, output, cache_dir)
        dense_batches.append(output.dense)
        if sparse_rows is not None:
            if output.sparse is None:
                raise ValueError(f"{config.code} expected sparse vectors")
            sparse_rows.extend(output.sparse)
        total_cost.add(output.cost)
        if batch_index == 1 or batch_index % 10 == 0 or batch_index == batch_count:
            print(
                f"[{config.code}] {role} batch {batch_index}/{batch_count} "
                f"(network calls this run={total_cost.network_api_calls_this_run}, cache hits={total_cost.cache_hit_batches})",
                file=sys.stderr,
                flush=True,
            )
    return EmbeddingOutput(np.concatenate(dense_batches, axis=0), sparse_rows, total_cost)


def embed_documents(
    client: DashScopeEmbeddingClient,
    config: EmbeddingConfig,
    texts: list[str],
    *,
    v0_by_text: dict[str, np.ndarray],
    cache_dir: Path,
    use_cache: bool,
) -> EmbeddingOutput:
    if config.code != "V0":
        return embed_remote(client, config, "document", texts, cache_dir=cache_dir, use_cache=use_cache)

    dense = np.empty((len(texts), config.dim), dtype=np.float32)
    missing_positions: list[int] = []
    missing_texts: list[str] = []
    db_cached = 0
    for index, text in enumerate(texts):
        vector = v0_by_text.get(text)
        if vector is None:
            missing_positions.append(index)
            missing_texts.append(text)
        else:
            dense[index] = vector
            db_cached += 1
    remote = embed_remote(
        client,
        config,
        "document",
        missing_texts,
        cache_dir=cache_dir,
        use_cache=use_cache,
    )
    for remote_index, destination in enumerate(missing_positions):
        dense[destination] = remote.dense[remote_index]
    remote.cost.db_cached_vectors += db_cached
    return EmbeddingOutput(dense, None, remote.cost)


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    values = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.where(norms > 0.0, norms, 1.0)


def _sparse_dot_matrix(
    query_sparse: list[dict[int, float]],
    document_sparse: list[dict[int, float]],
) -> np.ndarray:
    postings: dict[int, tuple[list[int], list[float]]] = {}
    for document_index, row in enumerate(document_sparse):
        for feature, value in row.items():
            document_ids, weights = postings.setdefault(feature, ([], []))
            document_ids.append(document_index)
            weights.append(value)
    output = np.zeros((len(query_sparse), len(document_sparse)), dtype=np.float32)
    for query_index, row in enumerate(query_sparse):
        for feature, query_weight in row.items():
            posting = postings.get(feature)
            if posting is None:
                continue
            document_ids, document_weights = posting
            output[query_index, np.asarray(document_ids, dtype=np.int64)] += query_weight * np.asarray(
                document_weights, dtype=np.float32
            )
    return output


def _pair_text(side: dict[str, Any], text_by_id: dict[str, str]) -> str:
    claim_id = str(side["claim_id"])
    if claim_id.startswith("synthetic:"):
        return str(side["value"])
    if claim_id not in text_by_id:
        raise ValueError(f"pair references missing claim: {claim_id}")
    return text_by_id[claim_id]


def _pair_metrics(
    rows: list[dict[str, Any]],
    normalized_documents: np.ndarray,
    document_index: dict[str, int],
    text_by_id: dict[str, str],
) -> dict[str, Any]:
    scores: list[float] = []
    labels: list[int] = []
    for row in rows:
        left_text = _pair_text(row["left"], text_by_id)
        right_text = _pair_text(row["right"], text_by_id)
        scores.append(
            float(normalized_documents[document_index[left_text]] @ normalized_documents[document_index[right_text]])
        )
        labels.append(int(row["label"] == "equivalent"))

    score_array = np.asarray(scores, dtype=np.float64)
    label_array = np.asarray(labels, dtype=np.int8)
    dev_mask = np.asarray([row["split"] == "dev" for row in rows])
    test_mask = np.asarray([row["split"] == "test" for row in rows])
    selected = select_threshold(label_array[dev_mask], score_array[dev_mask])
    selected_test = classification_metrics(label_array[test_mask], score_array[test_mask], selected["threshold"])
    fixed_thresholds = sorted(
        {
            0.50,
            0.60,
            0.70,
            0.75,
            0.80,
            0.82,
            0.85,
            0.88,
            0.90,
            0.92,
            0.95,
            round(float(selected["threshold"]), 12),
        }
    )
    return {
        "pr_auc": average_precision(label_array[test_mask], score_array[test_mask]),
        "best_f1": selected_test["f1"],
        "precision": selected_test["precision"],
        "recall": selected_test["recall"],
        "false_merge_rate": selected_test["false_merge_rate"],
        "threshold": selected_test["threshold"],
        "threshold_selection": "dev_max_f1",
        "dev_best_f1": selected["f1"],
        "threshold_metrics": [
            classification_metrics(label_array[test_mask], score_array[test_mask], threshold)
            for threshold in fixed_thresholds
        ],
        "test_pairs": int(test_mask.sum()),
        "test_positive_pairs": int(label_array[test_mask].sum()),
        "test_negative_pairs": int(test_mask.sum() - label_array[test_mask].sum()),
    }


def _rankings(
    rows: list[dict[str, Any]],
    claim_ids: list[str],
    scores: np.ndarray,
    *,
    confidence_scores: np.ndarray | None = None,
) -> tuple[dict[str, list[str]], dict[str, float]]:
    confidence = scores if confidence_scores is None else confidence_scores
    if confidence.shape != scores.shape:
        raise ValueError("ranking and confidence score matrices must have the same shape")
    rankings: dict[str, list[str]] = {}
    top1: dict[str, float] = {}
    for row, score_row, confidence_row in zip(rows, scores, confidence, strict=True):
        order = np.argsort(-score_row, kind="stable")
        rankings[row["id"]] = [claim_ids[int(index)] for index in order]
        top1[row["id"]] = float(confidence_row[int(order[0])])
    return rankings, top1


def _calibrated_recall_metrics(
    rows: list[dict[str, Any]],
    rankings: dict[str, list[str]],
    top1: dict[str, float],
    *,
    smoke: bool,
) -> dict[str, Any]:
    calibration_rows = rows if smoke else [row for row in rows if row["split"] == "dev"]
    evaluation_rows = rows if smoke else [row for row in rows if row["split"] == "test"]
    calibration_labels = np.asarray([int(row["no_answer"]) for row in calibration_rows], dtype=np.int8)
    calibration_scores = np.asarray([top1[row["id"]] for row in calibration_rows], dtype=np.float64)
    selected = select_threshold(calibration_labels, calibration_scores, positive_when_low=True)
    metrics = recall_metrics(
        evaluation_rows,
        rankings,
        top1,
        no_answer_threshold=float(selected["threshold"]),
        top_k=5,
    )
    metrics["threshold_selection"] = "smoke_self_calibration" if smoke else "dev_max_no_answer_f1"
    metrics["dev_no_answer_f1"] = selected["f1"]
    return metrics


def _select_smoke(
    rows: list[dict[str, Any]],
    corpus: list[ClaimRecord],
) -> tuple[list[dict[str, Any]], list[ClaimRecord]]:
    selected_rows = [
        next(row for row in rows if row["split"] == "dev" and not row["no_answer"]),
        next(row for row in rows if row["split"] == "dev" and row["no_answer"]),
        next(row for row in rows if row["split"] == "test" and not row["no_answer"]),
    ]
    active_by_id = {record.claim_id: record for record in corpus}
    selected_claims: list[ClaimRecord] = []
    selected_ids: set[str] = set()
    for row in selected_rows:
        for claim_id in row["gold_ids"]:
            if claim_id in active_by_id and claim_id not in selected_ids:
                selected_claims.append(active_by_id[claim_id])
                selected_ids.add(claim_id)
                break
    for record in corpus:
        if len(selected_claims) >= 3:
            break
        if record.claim_id not in selected_ids:
            selected_claims.append(record)
            selected_ids.add(record.claim_id)
    return selected_rows, selected_claims[:3]


def _unique_documents(
    corpus: list[ClaimRecord],
    pair_rows: list[dict[str, Any]],
    text_by_id: dict[str, str],
) -> tuple[list[str], dict[str, int]]:
    texts: list[str] = []
    index: dict[str, int] = {}

    def add(text: str) -> None:
        if text not in index:
            index[text] = len(texts)
            texts.append(text)

    for record in corpus:
        add(record.index_text)
    for row in pair_rows:
        add(_pair_text(row["left"], text_by_id))
        add(_pair_text(row["right"], text_by_id))
    return texts, index


def _run_config(
    config: EmbeddingConfig,
    client: DashScopeEmbeddingClient,
    *,
    pair_rows: list[dict[str, Any]],
    recall_rows: list[dict[str, Any]],
    corpus: list[ClaimRecord],
    database: DatabaseCorpus,
    cache_dir: Path,
    use_cache: bool,
    smoke: bool,
) -> dict[str, Any]:
    documents, document_index = _unique_documents(corpus, pair_rows, database.text_by_id)
    document_output = embed_documents(
        client,
        config,
        documents,
        v0_by_text=database.v0_by_text,
        cache_dir=cache_dir,
        use_cache=use_cache,
    )
    query_output = embed_remote(
        client,
        config,
        "query",
        [str(row["query"]) for row in recall_rows],
        cache_dir=cache_dir,
        use_cache=use_cache,
    )
    total_cost = Cost()
    total_cost.add(document_output.cost)
    total_cost.add(query_output.cost)

    normalized_documents = _normalize_rows(document_output.dense)
    normalized_queries = _normalize_rows(query_output.dense)
    corpus_document_indices = np.asarray([document_index[record.index_text] for record in corpus], dtype=np.int64)
    corpus_dense = normalized_documents[corpus_document_indices]
    dense_scores = normalized_queries @ corpus_dense.T

    claim_ids = [record.claim_id for record in corpus]
    primary_scores = dense_scores
    sparse_statistics: dict[str, Any] | None = None
    if config.use_sparse:
        if document_output.sparse is None or query_output.sparse is None:
            raise ValueError("Q4 sparse vectors are missing")
        corpus_sparse = [document_output.sparse[int(index)] for index in corpus_document_indices]
        sparse_scores = _sparse_dot_matrix(query_output.sparse, corpus_sparse)
        primary_scores = np.vstack(
            [
                rrf_fuse(dense_row, sparse_row, dense_weight=0.6, rank_constant=60)
                for dense_row, sparse_row in zip(dense_scores, sparse_scores, strict=True)
            ]
        )
        sparse_statistics = {
            "fusion": "rrf",
            "dense_weight": 0.6,
            "sparse_weight": 0.4,
            "rank_constant": 60,
            "document_nonzero_mean": float(np.mean([len(row) for row in corpus_sparse])),
            "query_nonzero_mean": float(np.mean([len(row) for row in query_output.sparse])),
        }

    rankings, top1 = _rankings(
        recall_rows,
        claim_ids,
        primary_scores,
        confidence_scores=dense_scores if config.use_sparse else None,
    )
    recall_result = _calibrated_recall_metrics(recall_rows, rankings, top1, smoke=smoke)
    if sparse_statistics is not None:
        recall_result["sparse"] = sparse_statistics
        dense_rankings, dense_top1 = _rankings(recall_rows, claim_ids, dense_scores)
        recall_result["dense_only_diagnostic"] = _calibrated_recall_metrics(
            recall_rows, dense_rankings, dense_top1, smoke=smoke
        )

    if smoke:
        pair_result: dict[str, Any] = {
            "status": "skipped_in_smoke",
            "reason": "smoke is intentionally limited to exactly 3 corpus claims and 3 recall queries",
        }
    else:
        pair_result = _pair_metrics(pair_rows, normalized_documents, document_index, database.text_by_id)

    result: dict[str, Any] = {
        "config": config.code,
        "model": config.model,
        "api": config.api_kind,
        "dim": config.dim,
        "pair_metrics": pair_result,
        "recall_metrics": recall_result,
        "cost": {
            **total_cost.as_dict(),
            "document_texts": len(documents),
            "query_texts": len(recall_rows),
        },
    }
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _best_by_metric(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    successful = [result for result in results if "error" not in result]
    if not successful or any(result["pair_metrics"].get("status") == "skipped_in_smoke" for result in successful):
        return {}
    paths = {
        "pair_pr_auc": lambda result: result["pair_metrics"]["pr_auc"],
        "pair_f1": lambda result: result["pair_metrics"]["best_f1"],
        "recall_hit_at_5": lambda result: result["recall_metrics"]["hit_at_5"],
        "recall_mrr": lambda result: result["recall_metrics"]["mrr"],
        "no_answer_precision_requested": lambda result: result["recall_metrics"]["no_answer_precision"],
        "answerability_balanced_accuracy": lambda result: result["recall_metrics"]["answerability_balanced_accuracy"],
    }
    output: dict[str, dict[str, Any]] = {}
    for metric, getter in paths.items():
        best_value = max(float(getter(result)) for result in successful)
        tied = [result["config"] for result in successful if abs(float(getter(result)) - best_value) <= 1e-12]
        output[metric] = {"value": best_value, "configs": tied}
    return output


def _parse_configs(value: str) -> list[EmbeddingConfig]:
    codes = list(CONFIGS) if value.strip().lower() == "all" else [item.strip().upper() for item in value.split(",")]
    unknown = [code for code in codes if code not in CONFIGS]
    if unknown:
        raise ValueError(f"unknown config(s): {', '.join(unknown)}")
    if len(set(codes)) != len(codes):
        raise ValueError("duplicate config code")
    return [CONFIGS[code] for code in codes]


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configs", default="all", help="Comma-separated V0,Q0,...,Q4 or 'all'")
    parser.add_argument("--smoke", action="store_true", help="Run exactly 3 active claims x 3 recall queries")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--strict-corpus", action="store_true", help="Fail if frozen and current corpus differ")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--trust-env", action="store_true", help="Let httpx use proxy variables from the environment")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_argument_parser().parse_args(argv)
    selected_configs = _parse_configs(arguments.configs)
    output_path = arguments.output or (DEFAULT_SMOKE_RESULT if arguments.smoke else DEFAULT_RESULT)
    pair_header, pair_rows = _read_jsonl(PAIR_DATASET)
    recall_header, recall_rows = _read_jsonl(RECALL_DATASET)
    if pair_header.get("corpus_fingerprint") != recall_header.get("corpus_fingerprint"):
        raise ValueError("frozen datasets disagree on corpus_fingerprint")
    if pair_header.get("corpus_count") != recall_header.get("corpus_count"):
        raise ValueError("frozen datasets disagree on corpus_count")

    database = load_database(arguments.db)
    expected_count = int(recall_header.get("corpus_count", 0))
    expected_fingerprint = recall_header.get("corpus_fingerprint", "")
    corpus_drift = expected_count != len(database.active) or expected_fingerprint != database.fingerprint
    if corpus_drift:
        message = (
            "frozen corpus drift: "
            f"expected count={expected_count} fingerprint={expected_fingerprint}, "
            f"actual count={len(database.active)} fingerprint={database.fingerprint}"
        )
        if arguments.strict_corpus:
            raise RuntimeError(message)
        print(f"WARNING: {message}", file=sys.stderr, flush=True)

    active_ids = {record.claim_id for record in database.active}
    missing_gold = sorted(
        {claim_id for row in recall_rows for claim_id in row["gold_ids"] if claim_id not in active_ids}
    )
    if missing_gold:
        raise RuntimeError(f"recall gold IDs are absent from active corpus: {missing_gold}")

    if arguments.smoke:
        recall_rows, corpus = _select_smoke(recall_rows, database.active)
        pair_rows = []
    else:
        corpus = database.active

    api_key = _load_env_value(ROOT / ".env", "EMBEDDING_API_KEY")
    if not api_key:
        raise RuntimeError("EMBEDDING_API_KEY is not set and was not found in .env")

    payload: dict[str, Any] = {
        "benchmark": "embedding_ablation_v1",
        "mode": "smoke" if arguments.smoke else "full",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configs_requested": [config.code for config in selected_configs],
        "datasets": {
            "claim_pair": {
                "path": str(PAIR_DATASET.relative_to(ROOT)),
                "sha256": _sha256_file(PAIR_DATASET),
                "rows": 80,
            },
            "recall": {
                "path": str(RECALL_DATASET.relative_to(ROOT)),
                "sha256": _sha256_file(RECALL_DATASET),
                "rows": 80,
            },
        },
        "corpus": {
            "expected_count": expected_count,
            "expected_fingerprint": expected_fingerprint,
            "actual_count": len(database.active),
            "actual_fingerprint": database.fingerprint,
            "evaluated_count": len(corpus),
            "corpus_drift": corpus_drift,
            "all_recall_gold_ids_active": True,
        },
        "policies": {
            "pair_threshold": "select on dev by max F1; report on test",
            "no_answer_threshold": "select on dev by max no-answer F1; report on test",
            "primary_recall_gold": "strict gold_ids",
            "q4_fusion": "0.6*dense_RRF + 0.4*sparse_RRF, rank_constant=60",
            "q4_no_answer_confidence": "dense cosine of the RRF-ranked top result; RRF rank scores are not calibrated relevance scores",
            "q2_q3_distinction": "Q2=text_type only; Q3=text_type plus query-side instruct",
        },
        "results": [],
        "best_by_metric": {},
        "complete": False,
    }
    _write_json(output_path, payload)

    client = DashScopeEmbeddingClient(
        api_key,
        timeout_seconds=arguments.timeout,
        max_attempts=arguments.max_attempts,
        trust_env=arguments.trust_env,
    )
    had_error = False
    try:
        for config in selected_configs:
            print(f"Starting {config.code} ({config.model}, {config.api_kind})", file=sys.stderr, flush=True)
            try:
                result = _run_config(
                    config,
                    client,
                    pair_rows=pair_rows,
                    recall_rows=recall_rows,
                    corpus=corpus,
                    database=database,
                    cache_dir=arguments.cache_dir,
                    use_cache=not arguments.no_cache,
                    smoke=arguments.smoke,
                )
            except Exception as error:  # Keep earlier configs checkpointed for expensive full runs.
                had_error = True
                result = {
                    "config": config.code,
                    "model": config.model,
                    "api": config.api_kind,
                    "dim": config.dim,
                    "error": f"{type(error).__name__}: {error}",
                }
                print(f"ERROR {config.code}: {result['error']}", file=sys.stderr, flush=True)
            payload["results"].append(result)
            payload["best_by_metric"] = _best_by_metric(payload["results"])
            _write_json(output_path, payload)
    finally:
        client.close()

    payload["complete"] = not had_error and len(payload["results"]) == len(selected_configs)
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()
    payload["best_by_metric"] = _best_by_metric(payload["results"])
    _write_json(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
