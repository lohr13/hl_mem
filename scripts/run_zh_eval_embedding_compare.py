#!/usr/bin/env python
"""Compare six embedding configurations on the Chinese memory evaluation set."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hl_mem import __version__  # noqa: E402
from hl_mem.components import initialize_process, make_extractor  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402
from scripts.eval_against_gold import match_claims  # noqa: E402
from scripts.run_embedding_ablation import (  # noqa: E402
    Cost,
    DashScopeEmbeddingClient,
    EmbeddingConfig,
    embed_remote,
)

DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "zh_memory_eval.jsonl"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "zh_eval_embedding_compare.json"
DEFAULT_CONFIG = ROOT / "hl_mem.toml"
DEFAULT_ENV_FILE = ROOT / ".env"
DEFAULT_EXTRACTION_CACHE = ROOT / "evaluation" / "cache" / "zh_eval_embedding_compare" / "extractions.json"
DEFAULT_EMBEDDING_CACHE = ROOT / "evaluation" / "cache" / "zh_eval_embedding_compare" / "embeddings"
RETRIEVAL_KS = (1, 5, 10)
PRIMARY_K = 5
VALUE_THRESHOLD = 0.62
EXPECTED_CASES = 50

EMBEDDING_CONFIGS: dict[str, dict[str, str | None]] = {
    "V0": {
        "model": "text-embedding-v4",
        "api": "compatible",
        "text_type": None,
        "output_type": "dense",
    },
    "Q0": {
        "model": "qwen3.7-text-embedding",
        "api": "compatible",
        "text_type": None,
        "output_type": "dense",
    },
    "Q1": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": None,
        "output_type": "dense",
    },
    "Q2": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": "query/document",
        "output_type": "dense",
    },
    "Q3": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": None,
        "output_type": "instruct",
    },
    "Q4": {
        "model": "qwen3.7-text-embedding",
        "api": "native",
        "text_type": "query/document",
        "output_type": "sparse",
    },
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--extraction-cache", type=Path, default=DEFAULT_EXTRACTION_CACHE)
    parser.add_argument("--embedding-cache", type=Path, default=DEFAULT_EMBEDDING_CACHE)
    parser.add_argument("--refresh-extraction", action="store_true")
    parser.add_argument("--value-threshold", type=float, default=VALUE_THRESHOLD)
    return parser.parse_args(argv)


def load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Chinese evaluation dataset does not exist: {path}")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    case_ids = [str(case.get("case_id") or "") for case in cases]
    if len(case_ids) != len(set(case_ids)) or any(not case_id for case_id in case_ids):
        raise ValueError("dataset case_id values must be present and unique")
    return cases


def _dataset_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_extraction_content(case: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten sessions for one extraction call while retaining boundaries."""
    raw_messages = case.get("conversation") or []
    if isinstance(raw_messages, (str, bytes)) or not isinstance(raw_messages, Sequence):
        raise ValueError(f"case {case.get('case_id')}: conversation must be a list")
    messages: list[dict[str, str]] = []
    text_lines: list[str] = []
    previous_session: str | None = None
    for raw_message in raw_messages:
        if not isinstance(raw_message, Mapping):
            raise ValueError(f"case {case.get('case_id')}: conversation entries must be objects")
        role = str(raw_message.get("role") or "user")
        content = str(raw_message.get("content") or "")
        session_id = str(raw_message.get("session_id") or "s01")
        if session_id != previous_session:
            text_lines.append(f"[Session {session_id}]")
            previous_session = session_id
        text_lines.append(f"{role}: {content}")
        messages.append({"role": role, "content": content})
    return {"text": "\n".join(text_lines), "messages": messages}


def _claim_to_dict(claim: object) -> dict[str, Any]:
    if isinstance(claim, Mapping):
        return dict(claim)
    if is_dataclass(claim):
        return asdict(claim)
    model_dump = getattr(claim, "model_dump", None)
    if callable(model_dump):
        value = model_dump()
        if isinstance(value, Mapping):
            return dict(value)
    raise TypeError(f"unsupported extracted claim type: {type(claim).__name__}")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _cache_metadata(dataset_sha256: str, model: str, extractor_version: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_sha256": dataset_sha256,
        "model": model,
        "extractor_version": extractor_version,
    }


def extract_once(
    cases: Sequence[Mapping[str, Any]],
    extractor: Any,
    cache_path: Path,
    *,
    dataset_sha256: str,
    model: str,
    extractor_version: str,
    force: bool = False,
) -> dict[str, Any]:
    """Extract each case once and checkpoint after every successful case."""
    metadata = _cache_metadata(dataset_sha256, model, extractor_version)
    payload: dict[str, Any] = {**metadata, "cases": {}}
    if cache_path.is_file() and not force:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if all(cached.get(key) == value for key, value in metadata.items()):
            payload = cached
    cached_cases = payload.setdefault("cases", {})
    if not isinstance(cached_cases, dict):
        raise ValueError("extraction cache cases must be an object")

    api_calls = 0
    cache_hits = 0
    started = time.perf_counter()
    for index, case in enumerate(cases, start=1):
        case_id = str(case["case_id"])
        cached_case = cached_cases.get(case_id)
        if isinstance(cached_case, Mapping) and isinstance(cached_case.get("claims"), list):
            cache_hits += 1
            print(f"[extract {index}/{len(cases)}] {case_id}: cache hit", flush=True)
            continue
        content = build_extraction_content(case)
        claims = extractor.extract(
            content,
            {
                "actor_type": "user",
                "event_type": "message",
                "session_id": case_id,
                "category": case.get("category"),
            },
        )
        api_calls += 1
        cached_cases[case_id] = {
            "category": case.get("category"),
            "claims": [_claim_to_dict(claim) for claim in claims],
            "usage": {
                "input_tokens": int(getattr(extractor, "last_input_tokens", 0)),
                "output_tokens": int(getattr(extractor, "last_output_tokens", 0)),
                "total_tokens": int(getattr(extractor, "last_usage_tokens", 0)),
            },
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(cache_path, payload)
        print(
            f"[extract {index}/{len(cases)}] {case_id}: claims={len(claims)} checkpointed",
            flush=True,
        )

    selected = {str(case["case_id"]): list(cached_cases[str(case["case_id"])]["claims"]) for case in cases}
    usage = Counter()
    for case in cases:
        record = cached_cases[str(case["case_id"])]
        usage.update({key: int(value) for key, value in (record.get("usage") or {}).items()})
    return {
        "claims_by_case": selected,
        "api_calls_this_run": api_calls,
        "cache_hits": cache_hits,
        "usage": dict(usage),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _f1(precision: float, recall: float) -> float:
    return 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0


def _extraction_counts(
    cases: Sequence[Mapping[str, Any]],
    claims_by_case: Mapping[str, Sequence[Mapping[str, Any]]],
    value_threshold: float,
) -> dict[str, Any]:
    gold_count = 0
    predicted_count = 0
    matched_count = 0
    should_correct = 0
    noise_over_extracted = 0
    case_details: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["case_id"])
        gold = list(case.get("gold_claims") or [])
        predicted = list(claims_by_case.get(case_id, []))
        matches = match_claims(gold, predicted, value_threshold=value_threshold)
        gold_count += len(gold)
        predicted_count += len(predicted)
        matched_count += len(matches)
        should_correct += int(bool(gold) == bool(predicted))
        if case.get("category") == "noise":
            noise_over_extracted += len(predicted)
        case_details.append(
            {
                "case_id": case_id,
                "category": case.get("category"),
                "gold_claims": len(gold),
                "predicted_claims": len(predicted),
                "matched_claims": len(matches),
                "missed": len(gold) - len(matches),
                "over_extracted": len(predicted) - len(matches),
            }
        )
    precision = matched_count / predicted_count if predicted_count else float(gold_count == 0)
    recall = matched_count / gold_count if gold_count else 1.0
    return {
        "cases": len(cases),
        "gold_claims": gold_count,
        "predicted_claims": predicted_count,
        "matched_claims": matched_count,
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "missed": gold_count - matched_count,
        "over_extracted": predicted_count - matched_count,
        "noise_over_extracted": noise_over_extracted,
        "should_extract_accuracy": should_correct / len(cases) if cases else 0.0,
        "case_details": case_details,
    }


def compute_extraction_metrics(
    cases: Sequence[Mapping[str, Any]],
    claims_by_case: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    value_threshold: float,
) -> dict[str, Any]:
    metrics = _extraction_counts(cases, claims_by_case, value_threshold)
    by_category: dict[str, Any] = {}
    categories = sorted({str(case.get("category") or "uncategorized") for case in cases})
    for category in categories:
        selected = [case for case in cases if case.get("category") == category]
        category_metrics = _extraction_counts(selected, claims_by_case, value_threshold)
        category_metrics.pop("case_details")
        by_category[category] = category_metrics
    metrics["by_category"] = by_category
    return metrics


def _query_id(case_id: str, gold_index: int) -> str:
    return f"{case_id}:g{gold_index:03d}"


def _summarize_query_results(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    queries = len(records)
    result: dict[str, Any] = {"queries": queries}
    for k in RETRIEVAL_KS:
        hits = sum(bool(record[f"hit_at_{k}"]) for record in records)
        result[f"recall_at_{k}"] = hits / queries if queries else 0.0
    result["mrr"] = sum(float(record["reciprocal_rank"]) for record in records) / queries if queries else 0.0
    primary_hits = sum(bool(record[f"hit_at_{PRIMARY_K}"]) for record in records)
    returned = sum(int(record[f"returned_at_{PRIMARY_K}"]) for record in records)
    precision = primary_hits / returned if returned else 0.0
    recall = primary_hits / queries if queries else 0.0
    result[f"precision_at_{PRIMARY_K}"] = precision
    result[f"f1_at_{PRIMARY_K}"] = _f1(precision, recall)
    result["matched_queries"] = primary_hits
    result["returned_candidates"] = returned
    return result


def evaluate_retrieval_rankings(
    cases: Sequence[Mapping[str, Any]],
    claims_by_case: Mapping[str, Sequence[Mapping[str, Any]]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    value_threshold: float,
) -> dict[str, Any]:
    """Judge ranked claims with the same subject/predicate/value matcher."""
    records: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["case_id"])
        predicted = list(claims_by_case.get(case_id, []))
        for gold_index, gold in enumerate(case.get("gold_claims") or []):
            ranked = list(rankings.get(_query_id(case_id, gold_index), predicted[:0]))
            first_rank = next(
                (
                    rank
                    for rank, claim in enumerate(ranked, start=1)
                    if match_claims([gold], [claim], value_threshold=value_threshold)
                ),
                None,
            )
            record: dict[str, Any] = {
                "query_id": _query_id(case_id, gold_index),
                "case_id": case_id,
                "category": case.get("category"),
                "first_relevant_rank": first_rank,
                "reciprocal_rank": 1.0 / first_rank if first_rank else 0.0,
            }
            for k in RETRIEVAL_KS:
                record[f"hit_at_{k}"] = bool(first_rank and first_rank <= k)
                record[f"returned_at_{k}"] = min(k, len(ranked))
            records.append(record)
    metrics = _summarize_query_results(records)
    by_category: dict[str, Any] = {}
    for category in sorted({str(record["category"]) for record in records}):
        by_category[category] = _summarize_query_results(
            [record for record in records if record["category"] == category]
        )
    metrics["by_category"] = by_category
    metrics["query_details"] = records
    return metrics


def _embedding_config(code: str) -> EmbeddingConfig:
    definition = EMBEDDING_CONFIGS[code]
    output_type = definition["output_type"]
    return EmbeddingConfig(
        code=code,
        model=str(definition["model"]),
        api_kind=str(definition["api"]),
        dim=2048,
        batch_size=20 if code == "Q0" else 10,
        use_text_type=definition["text_type"] == "query/document",
        use_instruct=output_type == "instruct",
        use_sparse=output_type == "sparse",
    )


def _claim_index_text(claim: Mapping[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            str(claim.get("subject") or "").strip(),
            str(claim.get("predicate") or "").strip(),
            str(claim.get("value") or "").strip(),
        )
        if part
    )


def _normalized_rows(matrix: np.ndarray) -> np.ndarray:
    values = np.asarray(matrix, dtype=np.float32)
    if values.size == 0:
        return values
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.where(norms > 0.0, norms, 1.0)


def _embed_and_rank(
    cases: Sequence[Mapping[str, Any]],
    claims_by_case: Mapping[str, Sequence[Mapping[str, Any]]],
    client: DashScopeEmbeddingClient,
    config: EmbeddingConfig,
    cache_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    document_positions: dict[str, list[int]] = defaultdict(list)
    for case in cases:
        case_id = str(case["case_id"])
        for claim in claims_by_case.get(case_id, []):
            document_positions[case_id].append(len(documents))
            documents.append(dict(claim))
    queries: list[tuple[str, str]] = []
    for case in cases:
        case_id = str(case["case_id"])
        for gold_index, gold in enumerate(case.get("gold_claims") or []):
            queries.append((_query_id(case_id, gold_index), str(gold.get("value") or "")))

    document_output = embed_remote(
        client,
        config,
        "document",
        [_claim_index_text(claim) for claim in documents],
        cache_dir=cache_dir,
        use_cache=True,
    )
    query_output = embed_remote(
        client,
        config,
        "query",
        [text for _, text in queries],
        cache_dir=cache_dir,
        use_cache=True,
    )
    cost = Cost()
    cost.add(document_output.cost)
    cost.add(query_output.cost)
    normalized_documents = _normalized_rows(document_output.dense)
    normalized_queries = _normalized_rows(query_output.dense)

    rankings: dict[str, list[dict[str, Any]]] = {}
    for query_position, (query_id, _) in enumerate(queries):
        case_id = query_id.split(":g", 1)[0]
        positions = document_positions.get(case_id, [])
        if not positions:
            rankings[query_id] = []
            continue
        scores = normalized_documents[positions] @ normalized_queries[query_position]
        order = sorted(range(len(positions)), key=lambda index: (-float(scores[index]), index))
        rankings[query_id] = [documents[positions[index]] for index in order]

    sparse_rows = sum(len(rows) if rows is not None else 0 for rows in (document_output.sparse, query_output.sparse))
    return rankings, {
        "documents": len(documents),
        "queries": len(queries),
        "cost": cost.as_dict(),
        "sparse_requested": config.use_sparse,
        "sparse_rows_received": sparse_rows,
        "retrieval_vector_mode": "dense_only",
    }


def _validate_settings(settings: Settings) -> None:
    if settings.llm_model != "qwen3.7-plus":
        raise ValueError(f"llm.model must be qwen3.7-plus, found {settings.llm_model}")
    if not settings.llm_api_key:
        raise ValueError("LLM_API_KEY is required")
    if not settings.embedding_api_key:
        raise ValueError("EMBEDDING_API_KEY is required")


def _comparison_rows(configs: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "precision",
        "recall",
        "f1",
        "retrieval_recall_at_1",
        "retrieval_recall_at_5",
        "retrieval_recall_at_10",
        "retrieval_mrr",
    )
    return [{"metric": field, **{code: payload.get(field) for code, payload in configs.items()}} for field in fields]


def _base_report(
    args: argparse.Namespace,
    settings: Settings,
    cases: Sequence[Mapping[str, Any]],
    dataset_sha256: str,
    extraction: Mapping[str, Any],
    extraction_metrics: Mapping[str, Any],
    configs: Mapping[str, Mapping[str, Any]],
    status: str,
) -> dict[str, Any]:
    claims_by_case = extraction["claims_by_case"]
    return {
        "schema_version": 1,
        "benchmark": "zh_memory_eval_embedding_compare",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "package_version": f"v{__version__}",
        "dataset": {
            "path": str(args.dataset.resolve()),
            "sha256": dataset_sha256,
            "cases": len(cases),
            "gold_claims": sum(len(case.get("gold_claims") or []) for case in cases),
        },
        "method": {
            "extractor_model": settings.llm_model,
            "extractor_version": LLM_EXTRACTOR_VERSION,
            "value_threshold": args.value_threshold,
            "retrieval_query": "gold claim value",
            "retrieval_scope": "claims extracted from the same case",
            "primary_retrieval_k": PRIMARY_K,
            "matcher": "scripts.eval_against_gold.match_claims",
            "metric_note": (
                "Extraction precision/recall/F1 are shared across configs because re-embedding "
                "does not alter extracted claims; Recall@K and MRR select the embedding config."
            ),
            "q4_sparse": "requested but not indexed; dense component used for retrieval",
        },
        "extraction": {
            "cache": str(args.extraction_cache.resolve()),
            "api_calls_this_run": extraction["api_calls_this_run"],
            "cache_hits": extraction["cache_hits"],
            "usage": extraction["usage"],
            "elapsed_seconds": extraction["elapsed_seconds"],
            "metrics": extraction_metrics,
            "cases": [
                {
                    "case_id": case["case_id"],
                    "category": case.get("category"),
                    "claims": claims_by_case[str(case["case_id"])],
                }
                for case in cases
            ],
        },
        "configs": dict(configs),
        "comparison": _comparison_rows(configs),
    }


def _print_comparison(configs: Mapping[str, Mapping[str, Any]]) -> None:
    print(
        "| Config | Extract P | Extract R | Extract F1 | R@1 | R@5 | R@10 | MRR |",
        flush=True,
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|", flush=True)
    for code, result in configs.items():
        print(
            f"| {code} | {result['precision']:.1%} | {result['recall']:.1%} | "
            f"{result['f1']:.1%} | {result['retrieval_recall_at_1']:.1%} | "
            f"{result['retrieval_recall_at_5']:.1%} | "
            f"{result['retrieval_recall_at_10']:.1%} | {result['retrieval_mrr']:.4f} |",
            flush=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0.0 <= args.value_threshold <= 1.0:
        raise ValueError("--value-threshold must be between 0 and 1")
    cases = load_cases(args.dataset)
    if len(cases) != EXPECTED_CASES:
        raise ValueError(f"expected {EXPECTED_CASES} cases, found {len(cases)}")
    dataset_sha256 = _dataset_sha256(args.dataset)
    settings = load_settings(args.config, args.env_file)
    _validate_settings(settings)
    initialize_process(settings)
    extractor = make_extractor(settings, require_real=True)
    prompt_identity = f"{LLM_EXTRACTOR_VERSION}:{getattr(extractor, 'prompt_hash', 'unknown')}"

    extraction = extract_once(
        cases,
        extractor,
        args.extraction_cache,
        dataset_sha256=dataset_sha256,
        model=settings.llm_model,
        extractor_version=prompt_identity,
        force=args.refresh_extraction,
    )
    claims_by_case = extraction["claims_by_case"]
    extraction_metrics = compute_extraction_metrics(
        cases,
        claims_by_case,
        value_threshold=args.value_threshold,
    )
    print(
        "extraction "
        f"P={extraction_metrics['precision']:.1%} "
        f"R={extraction_metrics['recall']:.1%} "
        f"F1={extraction_metrics['f1']:.1%} "
        f"predicted={extraction_metrics['predicted_claims']} "
        f"noise_over={extraction_metrics['noise_over_extracted']}",
        flush=True,
    )

    configs: dict[str, dict[str, Any]] = {}
    client = DashScopeEmbeddingClient(
        str(settings.embedding_api_key),
        base_url=settings.embedding_base_url,
        timeout_seconds=max(90.0, settings.embedding_read_timeout),
        max_attempts=settings.embedding_max_attempts,
        trust_env=False,
    )
    try:
        for code, definition in EMBEDDING_CONFIGS.items():
            config = _embedding_config(code)
            print(
                f"[{code}] model={config.model} api={config.api_kind} "
                f"text_type={config.use_text_type} instruct={config.use_instruct} "
                f"sparse={config.use_sparse}",
                flush=True,
            )
            started = time.perf_counter()
            rankings, embedding_stats = _embed_and_rank(
                cases,
                claims_by_case,
                client,
                config,
                args.embedding_cache,
            )
            retrieval = evaluate_retrieval_rankings(
                cases,
                claims_by_case,
                rankings,
                value_threshold=args.value_threshold,
            )
            configs[code] = {
                "definition": dict(definition),
                "precision": extraction_metrics["precision"],
                "recall": extraction_metrics["recall"],
                "f1": extraction_metrics["f1"],
                "embedding_invariant_extraction_metrics": True,
                "retrieval_recall_at_1": retrieval["recall_at_1"],
                "retrieval_recall_at_5": retrieval["recall_at_5"],
                "retrieval_recall_at_10": retrieval["recall_at_10"],
                "retrieval_mrr": retrieval["mrr"],
                "retrieval_precision_at_5": retrieval["precision_at_5"],
                "retrieval_f1_at_5": retrieval["f1_at_5"],
                "retrieval": retrieval,
                "embedding": embedding_stats,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
            report = _base_report(
                args,
                settings,
                cases,
                dataset_sha256,
                extraction,
                extraction_metrics,
                configs,
                "running",
            )
            _write_json_atomic(args.output, report)
            print(
                f"[{code}] R@1={retrieval['recall_at_1']:.1%} "
                f"R@5={retrieval['recall_at_5']:.1%} "
                f"MRR={retrieval['mrr']:.4f}",
                flush=True,
            )
    finally:
        client.close()

    report = _base_report(
        args,
        settings,
        cases,
        dataset_sha256,
        extraction,
        extraction_metrics,
        configs,
        "completed",
    )
    ordered = sorted(
        configs,
        key=lambda code: (
            -configs[code]["retrieval_recall_at_5"],
            -configs[code]["retrieval_mrr"],
            -configs[code]["retrieval_recall_at_1"],
            code,
        ),
    )
    report["selection_ranking"] = ordered
    report["recommended_config"] = ordered[0] if ordered else None
    _write_json_atomic(args.output, report)
    _print_comparison(configs)
    print(f"recommended={report['recommended_config']} output={args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
