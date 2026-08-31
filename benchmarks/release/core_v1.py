"""Run the frozen zero-network Core 1.0 benchmark through public REST endpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any, Iterator

from fastapi.testclient import TestClient

import hl_mem
from hl_mem.api.server import create_app
from hl_mem.ingest.embedder import Embedder
from hl_mem.ingest.image_describer import GovernedImageDescriber
from hl_mem.llm.client import LLMClient
from hl_mem.recall.reranker import DashScopeReranker
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "tests" / "eval" / "public" / "recall_core_v1.jsonl"
DEFAULT_PROTOCOL = Path(__file__).with_name("core_v1_protocol.json")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _sha256_utf8_lf(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("query"), str):
            raise ValueError(f"{path}:{line_number} is not a valid benchmark case")
        if row.get("expected_type") == "claim" and not row.get("expected_keywords"):
            raise ValueError(f"{path}:{line_number} has no expected keywords")
        rows.append(row)
    if len(rows) != 32:
        raise ValueError(f"Core 1.0 dataset must contain 32 cases, got {len(rows)}")
    return rows


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _matches(text: object, keywords: list[str], mode: str) -> bool:
    normalized = _normalized(text)
    checks = [_normalized(keyword) in normalized for keyword in keywords]
    return all(checks) if mode == "all" else any(checks)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)] if ordered else 0.0


def _classification_metrics(records: list[dict[str, Any]], prediction: str) -> tuple[float, float]:
    actual = [record for record in records if not record["answerable"]]
    predicted = [record for record in records if record[prediction]]
    true_positive = [record for record in actual if record[prediction]]
    precision = len(true_positive) / len(predicted) if predicted else 0.0
    recall = len(true_positive) / len(actual) if actual else 0.0
    return precision, recall


@contextmanager
def _reject_external_model_calls(counter: list[int]) -> Iterator[None]:
    patched = (
        (LLMClient, "complete"),
        (Embedder, "embed_batch"),
        (DashScopeReranker, "rerank"),
        (GovernedImageDescriber, "describe"),
    )
    originals = [(owner, name, getattr(owner, name)) for owner, name in patched]

    def unexpected(*_args: object, **_kwargs: object) -> None:
        counter[0] += 1
        raise RuntimeError("Core 1.0 public benchmark attempted an external model call")

    try:
        for owner, name, _original in originals:
            setattr(owner, name, unexpected)
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


def _memory_text(row: dict[str, Any]) -> str:
    values = [str(row["query"]), *(str(value) for value in row["expected_keywords"])]
    return " ".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def _run(rows: list[dict[str, Any]], protocol: dict[str, Any]) -> dict[str, Any]:
    top_k = 5
    model_calls = [0]
    records: list[dict[str, Any]] = []
    latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="hl-mem-core-v1-") as temporary_directory:
        settings = replace(
            Settings.for_test(),
            database_path=str(Path(temporary_directory) / "core-v1.db"),
            embedder_mode="fake",
            embedding_dim=2048,
            extractor_mode="fake",
            reranker_mode="off",
            index_text_mode="legacy",
        )
        with _reject_external_model_calls(model_calls), TestClient(create_app(settings)) as client:
            answerable = [row for row in rows if row.get("expected_type") == "claim"]
            for row in answerable:
                response = client.post(
                    "/v1/memories",
                    json={
                        "text": _memory_text(row),
                        "idempotency_key": f"core-v1:{row['id']}",
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError(f"{row['id']}: memory write failed with HTTP {response.status_code}")

            worker = Worker(settings)
            try:
                for _index in range(len(answerable) * 3 + 10):
                    outcome = worker.run_once()
                    if outcome.get("status") == "idle":
                        break
                    if outcome.get("status") != "succeeded":
                        raise RuntimeError(f"worker failed during fixture construction: {outcome}")
                else:
                    raise RuntimeError("worker did not drain the public benchmark fixture")
            finally:
                worker.close()

            for row in rows:
                payload = {
                    "query": row["query"],
                    "intent": row.get("intent", "current_state"),
                    "limit": top_k,
                    "debug": True,
                }
                started = time.perf_counter()
                response = client.post("/v1/recall", json=payload)
                latency_ms = (time.perf_counter() - started) * 1000.0
                latencies.append(latency_ms)
                body = response.json()
                results = body.get("results", []) if isinstance(body, dict) else []
                if not isinstance(results, list):
                    results = []
                keywords = [str(value) for value in row.get("expected_keywords", [])]
                ranks = [
                    rank
                    for rank, result in enumerate(results[:top_k], start=1)
                    if isinstance(result, dict)
                    and _matches(result.get("text"), keywords, str(row.get("keyword_match", "all")))
                ]
                answerability = str(body.get("answerability") or "supported") if isinstance(body, dict) else "error"
                forbidden_statuses = {str(value) for value in row.get("forbidden_statuses", [])}
                forbidden_hits = [
                    str(result.get("id"))
                    for result in results[:top_k]
                    if isinstance(result, dict) and str(result.get("status")) in forbidden_statuses
                ]
                is_answerable = row.get("expected_type") == "claim"
                records.append(
                    {
                        "id": str(row["id"]),
                        "slice": str(row["slice"]),
                        "answerable": is_answerable,
                        "hit_at_1": bool(ranks and ranks[0] == 1) if is_answerable else None,
                        "hit_at_5": bool(ranks) if is_answerable else None,
                        "mrr": (1.0 / ranks[0] if ranks else 0.0) if is_answerable else None,
                        "hard_abstention": answerability == "no_evidence",
                        "soft_abstention": answerability == "low_confidence",
                        "forbidden_hits": forbidden_hits,
                        "http_status": response.status_code,
                    }
                )

    answerable_records = [record for record in records if record["answerable"]]
    hard_precision, hard_recall = _classification_metrics(records, "hard_abstention")
    soft_precision, soft_recall = _classification_metrics(records, "soft_abstention")
    return {
        "case_count": len(records),
        "metrics": {
            "recall_at_1": mean(float(record["hit_at_1"]) for record in answerable_records),
            "recall_at_5": mean(float(record["hit_at_5"]) for record in answerable_records),
            "mrr": mean(float(record["mrr"]) for record in answerable_records),
            "hard_abstention_precision": hard_precision,
            "hard_abstention_recall": hard_recall,
            "soft_abstention_precision": soft_precision,
            "soft_abstention_recall": soft_recall,
        },
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
        "http_success_rate": sum(record["http_status"] == 200 for record in records) / len(records),
        "total_forbidden_hits": sum(len(record["forbidden_hits"]) for record in records),
        "external_model_calls": model_calls[0],
        "queries": records,
        "protocol_version": protocol["protocol_version"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--label", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if not COMMIT_PATTERN.fullmatch(arguments.commit):
            raise ValueError("commit must be a lowercase 40-hex Git object ID")
        protocol = _load_object(arguments.protocol)
        rows = _load_rows(arguments.dataset)
        result = _run(rows, protocol)
        result.update(
            {
                "schema_version": 1,
                "label": arguments.label,
                "commit": arguments.commit,
                "package_version": hl_mem.__version__,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "dataset_sha256": _sha256_utf8_lf(arguments.dataset),
                "protocol_sha256": _sha256_utf8_lf(arguments.protocol),
            }
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"Core 1.0 public benchmark failed: {error}")
        return 1
    print(
        "Core 1.0 public benchmark passed | "
        f"cases={result['case_count']} recall@5={result['metrics']['recall_at_5']:.4f} "
        f"mrr={result['metrics']['mrr']:.4f} p95={result['latency_ms']['p95']:.1f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
