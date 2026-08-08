#!/usr/bin/env python
"""Run PerLTQA recall benchmark.

Pipeline per character:
  1. Direct insert: memory claims (from perltmem.json) → hl_mem DB (no LLM extraction)
  2. Recall: each QA question → hl_mem recall → retrieved claims
  3. Score: Recall@5, MRR — check if gold claim (by source_key) appears in top-K

Unlike MemDaily, this benchmark does NOT use LLM extraction or QA answering.
It tests pure recall: can the embedding+reranker pipeline find the right memory
given a question?
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import json
import re
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hl_mem import __version__  # noqa: E402
from hl_mem.application.ingest import new_id  # noqa: E402
from hl_mem.application.recall import RecallService  # noqa: E402
from hl_mem.components import (  # noqa: E402
    initialize_process,
    make_embedder,
    make_query_expander,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.domain.claims.claim import build_index_text  # noqa: E402
from hl_mem.evaluation.perltqa import (  # noqa: E402
    PerLTQAAdapter,
    PerLTQACharacter,
    PerLTQAQuestion,
)
from hl_mem.recall.relation_expansion import RelationExpansionConfig  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402
from hl_mem.storage.claims import ClaimRepository  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402

DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "perltqa_smoke.json"
DEFAULT_CONFIG = ROOT / "hl_mem.toml"
DEFAULT_ENV_FILE = ROOT / ".env"
DATABASE_ROOT = ROOT / "var" / "benchmark_perltqa"
RECALL_K = 10  # recall limit; recall@5 computed from top-5


# ─── DB helpers ───────────────────────────────────────────────────────────────


def _safe_case_name(name: str) -> str:
    """Convert character name to safe filename."""
    # Use a hash for safety with Chinese characters
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:64]
    if not safe:
        import hashlib

        safe = hashlib.md5(name.encode("utf-8")).hexdigest()[:16]
    return safe


def _character_db_path(char_name: str) -> Path:
    return DATABASE_ROOT / f"{_safe_case_name(char_name)}.db"


def _remove_db_artifacts(db_path: Path) -> None:
    """Remove database and WAL/SHM files."""
    root = DATABASE_ROOT.resolve()
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            if p.resolve().is_relative_to(root):
                p.unlink(missing_ok=True)
        except PermissionError:
            raise
        except (OSError, ValueError):
            pass


# ─── Direct claim insertion (no extraction) ──────────────────────────────────


def _build_and_insert_claims(
    connection: Any,
    character: PerLTQACharacter,
    settings: Settings,
    embedder: Any,
) -> dict[str, str]:
    """Insert memory claims directly into DB, return {source_key: claim_id}.

    Each claim is inserted with all required fields for recall to work:
    - index_text, embedding_dense, namespace_key, status=active, etc.
    - benchmark_locator in qualifiers records source_key for gold binding
    """
    repo = ClaimRepository(connection, settings=settings)
    now = datetime.now(timezone.utc).isoformat()
    namespace = f"eval:perltqa:{_safe_case_name(character.name)}"

    source_key_to_claim_id: dict[str, str] = {}

    for claim_spec in character.claims:
        claim_id = new_id()
        claim_text = claim_spec.text

        # Build the claim dict matching the claims table schema
        claim: dict[str, Any] = {
            "id": claim_id,
            "namespace_key": namespace,
            "subject_entity_id": character.name,
            "predicate": claim_spec.category,
            "value": claim_text,
            "canonical_attribute": "custom.perltqa",
            "canonical_slot": None,
            "topic_tags_json": json.dumps([claim_spec.category, "perltqa"], ensure_ascii=False),
            "occurred_start": None,
            "occurred_end": None,
            "entities_json": None,
            "fact_hash": f"perltqa:{character.name}:{claim_spec.source_key}",
            "qualifiers": {
                "benchmark_locator": {
                    "source_key": claim_spec.source_key,
                    "category": claim_spec.category,
                    "character": character.name,
                }
            },
            "conflict_key": f"perltqa:{character.name}:{claim_spec.source_key}",
            "conflict_key_version": 3,
            "legacy_conflict_key": None,
            "valid_from": now,
            "recorded_from": now,
            "observed_at": now,
            "expires_at": None,
            "volatility": "stable",
            "status": "active",
            "confidence": 1.0,
            "scope": "permanent",
            "importance": 0.8,
            "access_count": 0,
            "last_accessed_at": None,
            "source_authority": "high",
            "extractor_version": "perltqa-direct-v1",
            "embedding_model": getattr(embedder, "model", "fake"),
            "embedding_dim": embedder.dim,
        }

        # Build index_text for FTS + use as embedding source
        claim["index_text"] = build_index_text(
            {**claim, "topic_tags": [claim_spec.category, "perltqa"]},
            mode=settings.index_text_mode,
        )
        claim["embedding_dense"] = embedder.embed_one(claim["index_text"])

        # Insert directly via repository (handles FTS v2 + encoding)
        repo.insert_claim(claim, commit=True)

        source_key_to_claim_id[claim_spec.source_key] = claim_id

    return source_key_to_claim_id


def _map_gold_claim_ids(
    reference_keys: Sequence[str],
    source_key_to_claim_id: Mapping[str, str],
) -> list[str]:
    """Resolve exact source keys, then fall back from a dialogue ``#N`` ordinal."""
    claim_ids: list[str] = []
    seen: set[str] = set()
    for reference_key in reference_keys:
        claim_id = source_key_to_claim_id.get(reference_key)
        if claim_id is None:
            aggregate_key, separator, ordinal = reference_key.rpartition("#")
            if separator and ordinal.isdigit():
                claim_id = source_key_to_claim_id.get(aggregate_key)
        if claim_id is not None and claim_id not in seen:
            seen.add(claim_id)
            claim_ids.append(claim_id)
    return claim_ids


# ─── Recall + scoring ────────────────────────────────────────────────────────


def _recall_question(
    connection: Any,
    question: PerLTQAQuestion,
    character: PerLTQACharacter,
    settings: Settings,
    embedder: Any,
    reranker: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run recall for one question, return metrics + retrieved claims."""
    namespace = f"eval:perltqa:{_safe_case_name(character.name)}"
    service = RecallService(
        connection,
        embedder,
        reranker,
        RelationExpansionConfig(
            enabled=settings.relation_expansion_mode == "on",
            max_depth=settings.relation_expansion_max_depth,
        ),
        settings,
        make_query_expander(settings, connection),
    )

    started = time.perf_counter()
    response = service.recall(
        question.question,
        limit=RECALL_K,
        namespace=namespace,
        debug=True,
    )
    elapsed = time.perf_counter() - started

    raw_results = response.get("results") or []
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)]

    retrieved_payload: list[dict[str, Any]] = []
    for rank, result in enumerate(results, start=1):
        claim_id = str(result.get("id") or "")
        text = result.get("text") or ""
        score = result.get("score")
        retrieved_payload.append(
            {
                "rank": rank,
                "claim_id": claim_id,
                "text": text,
                "score": score,
            }
        )

    return (
        {
            "retrieved_claims": len(results),
            "elapsed_seconds": round(elapsed, 3),
        },
        retrieved_payload,
    )


def _score_recall(
    retrieved: Sequence[Mapping[str, Any]],
    gold_claim_ids: Sequence[str],
    k: int = 5,
) -> tuple[float, float]:
    """Compute Recall@k and MRR (reciprocal rank).

    Returns (recall_at_k, mrr).
    - recall_at_k: 1.0 if any gold claim in top-k, else 0.0
    - mrr: 1/rank of first gold claim, or 0.0 if not found
    """
    gold = set(gold_claim_ids)
    if not gold:
        return 0.0, 0.0

    recall_hit = False
    mrr = 0.0
    for rank, item in enumerate(retrieved, start=1):
        claim_id = str(item.get("claim_id") or "")
        if claim_id in gold:
            recall_hit = recall_hit or rank <= k
            if mrr == 0.0:
                mrr = 1.0 / rank
            break  # Found first gold — that's all we need for single-gold QA

    return (1.0 if recall_hit else 0.0), mrr


# ─── Character execution ─────────────────────────────────────────────────────


def _run_character(
    character: PerLTQACharacter,
    settings: Settings,
    embedder: Any,
    reranker: Any,
    *,
    case_number: int,
    total: int,
    clean: bool,
) -> dict[str, Any]:
    """Execute full pipeline for one character."""
    db_path = _character_db_path(character.name)
    result: dict[str, Any] = {
        "character": character.name,
        "claim_count": len(character.claims),
        "question_count": len(character.questions),
        "insert_stats": None,
        "questions": [],
        "error": None,
    }

    database: Database | None = None
    started = time.perf_counter()
    try:
        DATABASE_ROOT.mkdir(parents=True, exist_ok=True)
        _remove_db_artifacts(db_path)

        database = Database(db_path, settings=settings)
        connection = database.open()

        # Phase 1: Insert memory claims
        insert_started = time.perf_counter()
        source_key_to_claim_id = _build_and_insert_claims(connection, character, settings, embedder)
        result["insert_stats"] = {
            "claims_inserted": len(source_key_to_claim_id),
            "elapsed_seconds": round(time.perf_counter() - insert_started, 3),
        }

        print(
            f"[{case_number}/{total}] {character.name}: inserted "
            f"{len(source_key_to_claim_id)} claims, "
            f"testing {len(character.questions)} questions",
            flush=True,
        )

        # Phase 2: Recall each question
        for q_idx, question in enumerate(character.questions, start=1):
            # Map reference keys to gold claim IDs
            gold_claim_ids = _map_gold_claim_ids(question.reference_keys, source_key_to_claim_id)

            q_result: dict[str, Any] = {
                "category": question.category,
                "question": question.question,
                "answer": question.answer,
                "reference_keys": list(question.reference_keys),
                "gold_claim_ids": gold_claim_ids,
                "gold_claim_count": len(gold_claim_ids),
                "retrieval": None,
                "recall_at_5": 0.0,
                "mrr": 0.0,
                "error": None,
            }

            if not gold_claim_ids:
                q_result["error"] = "no_gold_claim_mapped"
                result["questions"].append(q_result)
                continue

            try:
                metrics, retrieved = _recall_question(connection, question, character, settings, embedder, reranker)
                recall_5, mrr = _score_recall(retrieved, gold_claim_ids, k=5)
                q_result["retrieval"] = metrics
                q_result["recall_at_5"] = recall_5
                q_result["mrr"] = round(mrr, 4)
                q_result["retrieved_top3"] = [
                    {"rank": r["rank"], "text": r["text"][:120], "score": r["score"]} for r in retrieved[:3]
                ]
            except Exception as e:
                q_result["error"] = f"{type(e).__name__}: {e}"

            result["questions"].append(q_result)

            if q_idx % 5 == 0 or q_idx == len(character.questions):
                print(
                    f"  [{case_number}/{total}] {character.name} Q{q_idx}/"
                    f"{len(character.questions)}: "
                    f"R@5={q_result['recall_at_5']} MRR={q_result['mrr']}",
                    flush=True,
                )

    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if database is not None:
            database.close()
        if clean:
            gc.collect()
            for attempt in range(3):
                try:
                    _remove_db_artifacts(db_path)
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.5)
                    else:
                        raise
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


# ─── Aggregation & reporting ─────────────────────────────────────────────────


def _aggregate_by_category(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate metrics by QA category."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

    for char_result in results:
        if char_result.get("error"):
            continue
        for q in char_result.get("questions", []):
            if q.get("error"):
                continue
            cat = str(q.get("category") or "unknown")
            grouped[cat].append(q)

    report: dict[str, dict[str, Any]] = {}
    for cat, questions in sorted(grouped.items()):
        r5_vals = [float(q["recall_at_5"]) for q in questions if q.get("recall_at_5") is not None]
        mrr_vals = [float(q["mrr"]) for q in questions if q.get("mrr") is not None]
        report[cat] = {
            "question_count": len(questions),
            "recall_at_5": round(mean(r5_vals), 4) if r5_vals else None,
            "mrr": round(mean(mrr_vals), 4) if mrr_vals else None,
        }

    return report


def _aggregate_overall(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute overall metrics across all questions."""
    all_r5: list[float] = []
    all_mrr: list[float] = []
    total_questions = 0
    question_errors = 0
    character_errors = 0

    for char_result in results:
        questions = char_result.get("questions") or []
        if char_result.get("error") or not questions:
            character_errors += 1
        for q in questions:
            total_questions += 1
            if q.get("error"):
                question_errors += 1
                continue
            if q.get("recall_at_5") is not None:
                all_r5.append(float(q["recall_at_5"]))
            if q.get("mrr") is not None:
                all_mrr.append(float(q["mrr"]))

    total_errors = question_errors + character_errors
    return {
        "total_characters": len(results),
        "total_questions": total_questions,
        "total_errors": total_errors,
        "character_errors": character_errors,
        "question_errors": question_errors,
        "successful_questions": total_questions - question_errors,
        "recall_at_5": round(mean(all_r5), 4) if all_r5 else None,
        "mrr": round(mean(all_mrr), 4) if all_mrr else None,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _build_report(
    characters: Sequence[PerLTQACharacter],
    results: Sequence[Mapping[str, Any]],
    settings: Settings,
    mem_source: Path,
    qa_source: Path,
    started_at: str,
    status: str,
    qa_per_category: int | None,
    per_character: int | None,
) -> dict[str, Any]:
    """Build the final report dict."""
    return {
        "schema_version": 1,
        "benchmark": "perltqa",
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "mem_source": str(mem_source.resolve()),
            "qa_source": str(qa_source.resolve()),
        },
        "run": {
            "started_at": started_at,
            "package_version": f"v{__version__}",
            "total_characters": len(characters),
            "per_character_limit": per_character,
            "qa_per_category_limit": qa_per_category,
            "models": {
                "embedder": settings.embedding_model,
                "reranker": settings.reranker_model if settings.reranker_mode != "off" else "off",
            },
        },
        "metrics": {
            "overall": _aggregate_overall(results),
            "by_category": _aggregate_by_category(results),
        },
        "characters": list(results),
    }


def _generate_markdown(report: Mapping[str, Any]) -> str:
    """Generate a human-readable Markdown summary."""
    overall = report.get("metrics", {}).get("overall", {})
    by_cat = report.get("metrics", {}).get("by_category", {})
    run_info = report.get("run", {})

    lines: list[str] = []
    lines.append("# PerLTQA Benchmark Report")
    lines.append("")
    lines.append(f"- **Benchmark**: {report.get('benchmark', 'perltqa')}")
    lines.append(f"- **Status**: {report.get('status', 'unknown')}")
    lines.append(f"- **Generated**: {report.get('generated_at', 'N/A')}")
    lines.append(f"- **Package version**: {run_info.get('package_version', 'N/A')}")
    lines.append(f"- **Characters**: {overall.get('total_characters', 'N/A')}")
    lines.append(f"- **Total questions**: {overall.get('total_questions', 'N/A')}")
    lines.append(f"- **Embedder**: {run_info.get('models', {}).get('embedder', 'N/A')}")
    lines.append(f"- **Reranker**: {run_info.get('models', {}).get('reranker', 'N/A')}")
    lines.append("")

    lines.append("## Overall Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    for key in ("total_characters", "total_questions", "successful_questions", "total_errors", "recall_at_5", "mrr"):
        val = overall.get(key)
        if val is None:
            val_str = "N/A"
        elif isinstance(val, float):
            val_str = f"{val:.4f}"
        else:
            val_str = str(val)
        lines.append(f"| {key} | {val_str} |")
    lines.append("")

    if by_cat:
        lines.append("## Metrics by Category")
        lines.append("")
        lines.append("| Category | Questions | Recall@5 | MRR |")
        lines.append("|----------|-----------|----------|-----|")
        for cat, group in sorted(by_cat.items()):
            r5 = group.get("recall_at_5")
            mrr = group.get("mrr")
            n = group.get("question_count", 0)
            r5_s = f"{r5:.4f}" if r5 is not None else "N/A"
            mrr_s = f"{mrr:.4f}" if mrr is not None else "N/A"
            lines.append(f"| {cat} | {n} | {r5_s} | {mrr_s} |")
        lines.append("")

    return "\n".join(lines) + "\n"


# ─── Main ────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to perltmem.json (memory data)",
    )
    parser.add_argument(
        "--qa-source",
        type=Path,
        required=True,
        help="Path to perltqa.json (QA data)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--per-character",
        type=int,
        default=3,
        help="Max characters to evaluate (default: 3 for smoke)",
    )
    parser.add_argument(
        "--qa-per-category",
        type=int,
        default=3,
        help="Max QA items per category per character (default: 3 for smoke)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clean", action="store_true", help="Remove DB files after each character")
    parser.add_argument("--no-clean", action="store_true", help="Keep DB files (debug)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.source.is_file():
        raise FileNotFoundError(f"PerLTQA memory source not found: {args.source}")
    if not args.qa_source.is_file():
        raise FileNotFoundError(f"PerLTQA QA source not found: {args.qa_source}")

    settings = load_settings(args.config, args.env_file)
    # Force sqlite_scan for benchmark DBs (small, no sqlite-vec dependency)
    settings = dataclasses.replace(
        settings,
        vector_backend="sqlite_scan",
        query_expansion_mode="off",
    )
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)

    adapter = PerLTQAAdapter()
    characters = adapter.load(
        args.source,
        args.qa_source,
        per_character=args.per_character,
        qa_per_category=args.qa_per_category,
    )
    if not characters:
        raise ValueError("No characters loaded — check source paths")

    total = len(characters)
    started_at = datetime.now(timezone.utc).isoformat()

    print(
        f"PerLTQA: {total} characters, "
        f"qa_per_category={args.qa_per_category}, "
        f"embedder={settings.embedding_model}, "
        f"reranker={'off' if settings.reranker_mode == 'off' else settings.reranker_model}",
        flush=True,
    )

    clean = args.clean or not args.no_clean  # default: clean
    results: list[dict[str, Any]] = []

    try:
        for case_number, character in enumerate(characters, start=1):
            case_result = _run_character(
                character,
                settings,
                embedder,
                reranker,
                case_number=case_number,
                total=total,
                clean=clean,
            )
            results.append(case_result)

            # Write incremental results
            _write_json_atomic(
                args.output,
                _build_report(
                    characters,
                    results,
                    settings,
                    args.source,
                    args.qa_source,
                    started_at,
                    "running",
                    args.qa_per_category,
                    args.per_character,
                ),
            )

            questions = case_result.get("questions", [])
            r5_vals = [q["recall_at_5"] for q in questions if q.get("recall_at_5") is not None]
            avg_r5 = mean(r5_vals) if r5_vals else 0.0
            print(
                f"[{case_number}/{total}] {character.name}: " f"avg_R@5={avg_r5:.3f} error={case_result.get('error')}",
                flush=True,
            )

    except Exception:
        if results:
            _write_json_atomic(
                args.output,
                _build_report(
                    characters,
                    results,
                    settings,
                    args.source,
                    args.qa_source,
                    started_at,
                    "aborted",
                    args.qa_per_category,
                    args.per_character,
                ),
            )
        raise

    report = _build_report(
        characters,
        results,
        settings,
        args.source,
        args.qa_source,
        started_at,
        "completed",
        args.qa_per_category,
        args.per_character,
    )
    _write_json_atomic(args.output, report)

    # Generate Markdown report
    md_path = args.output.with_suffix(".md")
    md_path.write_text(_generate_markdown(report), encoding="utf-8")

    overall = report["metrics"]["overall"]
    by_cat = report["metrics"]["by_category"]
    print("\n=== PerLTQA Results ===", flush=True)
    print(f"Characters: {overall['total_characters']}", flush=True)
    print(f"Questions: {overall['total_questions']} (errors: {overall['total_errors']})", flush=True)
    print(f"Overall Recall@5: {overall['recall_at_5']}", flush=True)
    print(f"Overall MRR: {overall['mrr']}", flush=True)
    for cat, metrics in sorted(by_cat.items()):
        print(
            f"  {cat}: R@5={metrics['recall_at_5']} MRR={metrics['mrr']} ({metrics['question_count']} Q)",
            flush=True,
        )
    print(f"Output: {args.output}", flush=True)

    return 1 if overall["total_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
