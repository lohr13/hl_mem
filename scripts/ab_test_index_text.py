"""Compare claim index-text projections on one frozen recall snapshot.

The command never writes to the source snapshot. It creates one frozen local
copy, derives isolated ``legacy`` and ``answerable`` databases from that copy,
rebuilds index_text/embeddings in each database, and invokes the Phase 0 recall
evaluation runner for both arms.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast

from hl_mem.components import make_embedder
from hl_mem.config_loader import load_settings
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.claim import IndexTextMode, build_index_text
from hl_mem.protocols import EmbedderProtocol
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.backfill_index_text import backfill_index_text

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPOSITORY_ROOT / "tests" / "eval" / "datasets" / "recall_v2.jsonl"


def _load_phase0_runner() -> Any:
    """Load the repository-owned Phase 0 runner when end-to-end mode is used."""
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    runner = importlib.import_module("tests.eval.eval_runner")
    runner_path = Path(str(getattr(runner, "__file__", ""))).resolve()
    if not runner_path.is_relative_to(REPOSITORY_ROOT / "tests"):
        raise RuntimeError(f"unexpected Phase 0 runner import: {runner_path}")
    return runner


INDEX_TEXT_MODES: tuple[IndexTextMode, ...] = (
    "legacy",
    "value_only",
    "natural",
    "answerable",
)
AB_INDEX_TEXT_MODES: tuple[IndexTextMode, ...] = ("legacy", "answerable")
RECALLABLE_STATUSES = ("active", "superseded", "expired")
DERIVED_CLAIM_COLUMNS = frozenset({"index_text", "embedding_dense", "embedding_model", "embedding_dim"})
PIPELINE_METRICS = (
    "hit_at_1",
    "hit_at_5",
    "recall_at_1",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "precision_at_3",
    "no_answer_precision",
    "no_answer_recall",
    "low_confidence_rate",
)

EvaluationRunner = Callable[[Path, Path, int, Settings | None], dict[str, Any]]


@dataclass(frozen=True)
class DiagnosticQuery:
    """A query and the conditions used to identify its target claim."""

    query: str
    target_terms: tuple[str, ...]
    target_claim_id: str | None = None


@dataclass(frozen=True)
class RankResult:
    """Dense target rank for one query and one projection mode."""

    query: str
    mode: IndexTextMode
    target_claim_id: str | None
    rank: int | None
    score: float | None


BUILTIN_QUERIES: tuple[DiagnosticQuery, ...] = (
    DiagnosticQuery("用户的技术栈和工具", ("技术栈", "工具", "Python", "PyTorch")),
    DiagnosticQuery("唇形同步项目", ("唇形同步", "lip-rt", "dhlive", "MuseTalk", "LatentSync")),
    DiagnosticQuery("数据清洗历史", ("数据清洗", "清洗")),
    DiagnosticQuery("GPU 硬件信息", ("GPU", "REDACTED_GPU", "CUDA")),
    DiagnosticQuery("hl_mem 服务配置", ("hl_mem", "配置", "端口")),
    DiagnosticQuery("Hermes 和 hl_mem 的关系", ("Hermes", "hl_mem")),
    DiagnosticQuery("用户偏好", ("偏好", "喜欢")),
    DiagnosticQuery("REDACTED_GPU", ("REDACTED_GPU",)),
    DiagnosticQuery("Codex 工作流", ("Codex", "工作流")),
    DiagnosticQuery("开源项目", ("开源", "项目")),
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_value(value: object) -> object:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"blob_sha256": hashlib.sha256(bytes(value)).hexdigest(), "length": len(value)}
    return value


def _canonical_digest(connection: sqlite3.Connection) -> str:
    """Hash canonical Claim/evidence state while excluding four rebuilt fields."""
    digest = hashlib.sha256()
    for table in ("claims", "evidence_links"):
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            if table != "claims" or str(row[1]) not in DERIVED_CLAIM_COLUMNS
        ]
        quoted_columns = ",".join(f'"{column}"' for column in columns)
        order_column = "id" if "id" in columns else columns[0]
        digest.update(json.dumps([table, columns], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        for row in connection.execute(f'SELECT {quoted_columns} FROM "{table}" ORDER BY "{order_column}"'):
            digest.update(
                json.dumps(
                    [_digest_value(value) for value in row],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def open_readonly_database(database_path: Path) -> sqlite3.Connection:
    """Open a SQLite database with filesystem writes disabled by SQLite."""
    resolved = database_path.resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _freeze_snapshot(source: Path, target: Path) -> None:
    """Create one transactionally consistent base copy via SQLite backup."""
    source_connection = open_readonly_database(source)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _claim_search_text(claim: dict[str, Any]) -> str:
    return json.dumps(claim, ensure_ascii=False, default=str).casefold()


def _select_target_claim(claims: Sequence[dict[str, Any]], diagnostic: DiagnosticQuery) -> str | None:
    if diagnostic.target_claim_id is not None:
        return (
            diagnostic.target_claim_id
            if any(claim.get("id") == diagnostic.target_claim_id for claim in claims)
            else None
        )
    scored: list[tuple[int, str]] = []
    for claim in claims:
        text = _claim_search_text(claim)
        matches = sum(term.casefold() in text for term in diagnostic.target_terms)
        if matches:
            scored.append((matches, str(claim["id"])))
    return max(scored, default=(0, ""))[1] or None


def compare_index_text_modes(
    claims: Sequence[dict[str, Any]],
    diagnostics: Sequence[DiagnosticQuery],
    embedder: EmbedderProtocol,
) -> list[RankResult]:
    """Keep the lightweight four-mode dense diagnostic API for local analysis."""
    target_ids = {diagnostic.query: _select_target_claim(claims, diagnostic) for diagnostic in diagnostics}
    query_vectors = {diagnostic.query: embedder.embed_one(diagnostic.query) for diagnostic in diagnostics}
    results: list[RankResult] = []
    for mode in INDEX_TEXT_MODES:
        ranked_vectors = [
            (str(claim["id"]), embedder.embed_one(build_index_text(claim, mode=mode))) for claim in claims
        ]
        for diagnostic in diagnostics:
            target_id = target_ids[diagnostic.query]
            scores = sorted(
                (
                    (
                        claim_id,
                        cosine_similarity(query_vectors[diagnostic.query], vector),
                    )
                    for claim_id, vector in ranked_vectors
                ),
                key=lambda item: (-item[1], item[0]),
            )
            target = next(
                ((rank, score) for rank, (claim_id, score) in enumerate(scores, start=1) if claim_id == target_id),
                None,
            )
            results.append(
                RankResult(
                    query=diagnostic.query,
                    mode=mode,
                    target_claim_id=target_id,
                    rank=target[0] if target else None,
                    score=target[1] if target else None,
                )
            )
    return results


def _load_diagnostics(path: Path | None) -> list[DiagnosticQuery]:
    if path is None:
        return list(BUILTIN_QUERIES)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("diagnostic set must be a JSON array")
    return [
        DiagnosticQuery(
            query=str(item["query"]),
            target_terms=tuple(str(term) for term in item.get("target_terms", [])),
            target_claim_id=str(item["target_claim_id"]) if item.get("target_claim_id") else None,
        )
        for item in payload
    ]


def _render_table(results: Sequence[RankResult]) -> str:
    lines = ["| 查询 | 模式 | 目标 claim | 排名 | cosine |", "|---|---|---|---:|---:|"]
    for result in results:
        score = f"{result.score:.6f}" if result.score is not None else "N/A"
        lines.append(
            f"| {result.query} | {result.mode} | {result.target_claim_id or '未找到'} | "
            f"{result.rank if result.rank is not None else 'N/A'} | {score} |"
        )
    return "\n".join(lines)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _verify_projection_copy(
    connection: sqlite3.Connection,
    mode: IndexTextMode,
    embedder: EmbedderProtocol,
) -> dict[str, Any]:
    repo = ClaimRepository(connection)
    claims = [claim for claim in repo.list_all() if claim.get("status") in RECALLABLE_STATUSES]
    invalid: list[dict[str, str]] = []
    for claim in claims:
        reasons: list[str] = []
        if claim.get("index_text") != build_index_text(claim, mode=mode):
            reasons.append("index_text")
        embedding = claim.get("embedding_dense")
        if not isinstance(embedding, bytes) or len(embedding) != 4 * embedder.dim:
            reasons.append("embedding_length")
        if claim.get("embedding_model") != embedder.model:
            reasons.append("embedding_model")
        if claim.get("embedding_dim") != embedder.dim:
            reasons.append("embedding_dim")
        if reasons:
            invalid.append({"claim_id": str(claim["id"]), "reasons": ",".join(reasons)})
    return {
        "eligible": len(claims),
        "complete": len(claims) - len(invalid),
        "invalid_count": len(invalid),
        "invalid_sample": invalid[:20],
    }


def _rebuild_projection_copy(
    database_path: Path,
    mode: IndexTextMode,
    settings: Settings,
    embedder: EmbedderProtocol,
) -> dict[str, Any]:
    """Force a full projection rebuild, but only inside an isolated copy."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        canonical_before = _canonical_digest(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE claims SET index_text=NULL,embedding_dense=NULL,embedding_model=NULL,embedding_dim=NULL "
            "WHERE status IN ('active','superseded','expired')"
        )
        if _table_exists(connection, "eval_fixture_metadata"):
            connection.execute(
                "INSERT INTO eval_fixture_metadata(key,value) VALUES ('index_text_mode',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (mode,),
            )
        connection.commit()
        summary = backfill_index_text(
            connection,
            embedder,
            mode=mode,
            version=settings.index_text_version,
            batch_size=settings.index_backfill_batch_size,
            max_attempts=settings.index_backfill_max_attempts,
        )
        coverage = _verify_projection_copy(connection, mode, embedder)
        if summary.failed or coverage["invalid_count"]:
            raise RuntimeError(
                f"{mode} projection rebuild incomplete: failed={summary.failed}, "
                f"invalid={coverage['invalid_count']}"
            )
        canonical_after = _canonical_digest(connection)
        if canonical_after != canonical_before:
            raise RuntimeError(f"{mode} projection rebuild changed canonical Claim/evidence state")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise RuntimeError(f"{mode} projection WAL checkpoint remained busy")
        return {
            "backfill": summary.to_dict(),
            "coverage": coverage,
            "canonical_digest": {
                "before": canonical_before,
                "after": canonical_after,
                "unchanged": True,
            },
        }
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _best_rank(items: Sequence[Mapping[str, Any]], relevant_ids: set[str]) -> tuple[int | None, str | None]:
    for rank, item in enumerate(items, 1):
        claim_id = str(item.get("id"))
        if claim_id in relevant_ids:
            return rank, claim_id
    return None, None


def _pipeline_by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in report.get("queries", [])}


def _channel_diagnostics(
    database_path: Path,
    rows: Sequence[dict[str, Any]],
    query_vectors: dict[str, bytes],
    pipeline_report: dict[str, Any],
    settings: Settings,
    reference_time: str,
) -> list[dict[str, Any]]:
    connection = open_readonly_database(database_path)
    try:
        repo = ClaimRepository(connection, vector_batch_size=settings.vector_batch_size, settings=settings)
        pipeline_queries = _pipeline_by_id(pipeline_report)
        results: list[dict[str, Any]] = []
        for row in rows:
            relevant_ids = {
                str(claim_id)
                for claim_id in (list(row.get("expected_claim_ids", [])) + list(row.get("equivalent_ids", [])))
            }
            namespace = str(row.get("namespace") or "default")
            as_of = row.get("as_of") or reference_time
            limit = settings.recall_vector_scan_limit
            fts = repo.search_claims_fts(
                str(row["query"]),
                limit,
                as_of,
                row.get("intent", "current_state"),
                row.get("known_as_of"),
                namespace,
            )
            dense = repo.search_claims_vector(
                query_vectors[str(row["query"])],
                limit,
                as_of,
                row.get("intent", "current_state"),
                row.get("known_as_of"),
                namespace,
            )
            fts_rank, fts_target = _best_rank(fts, relevant_ids)
            dense_rank, dense_target = _best_rank(dense, relevant_ids)
            dense_claim = next(
                (claim for claim in dense if str(claim.get("id")) == dense_target),
                None,
            )
            dense_cosine = (
                cosine_similarity(
                    query_vectors[str(row["query"])],
                    dense_claim["embedding_dense"],
                )
                if dense_claim is not None
                else None
            )
            pipeline = pipeline_queries.get(str(row["id"]), {})
            pipeline_items = [{"id": claim_id} for claim_id in pipeline.get("returned_ids", [])]
            pipeline_rank, pipeline_target = _best_rank(pipeline_items, relevant_ids)
            results.append(
                {
                    "id": str(row["id"]),
                    "query": str(row["query"]),
                    "slice": str(row.get("slice") or ""),
                    "relevant_claim_ids": sorted(relevant_ids),
                    "dense_rank": dense_rank,
                    "dense_cosine": dense_cosine,
                    "dense_target_claim_id": dense_target,
                    "fts_rank": fts_rank,
                    "fts_target_claim_id": fts_target,
                    "pipeline_rank": pipeline_rank,
                    "pipeline_target_claim_id": pipeline_target,
                    "pipeline_answerability": pipeline.get("answerability"),
                }
            )
        return results
    finally:
        connection.close()


def _metric_comparison(mode_reports: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    legacy_metrics = mode_reports["legacy"]["pipeline"]["metrics"]
    answerable_metrics = mode_reports["answerable"]["pipeline"]["metrics"]
    common_numeric = {
        metric
        for metric in set(legacy_metrics) & set(answerable_metrics)
        if isinstance(legacy_metrics[metric], (int, float))
        and not isinstance(legacy_metrics[metric], bool)
        and isinstance(answerable_metrics[metric], (int, float))
        and not isinstance(answerable_metrics[metric], bool)
    }
    ordered_metrics = [
        *[metric for metric in PIPELINE_METRICS if metric in common_numeric],
        *sorted(common_numeric - set(PIPELINE_METRICS)),
    ]
    return {
        metric: {
            "legacy": float(legacy_metrics[metric]),
            "answerable": float(answerable_metrics[metric]),
            "delta": float(answerable_metrics[metric]) - float(legacy_metrics[metric]),
        }
        for metric in ordered_metrics
    }


def _query_comparison(mode_reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_mode = {mode: {str(item["id"]): item for item in report["queries"]} for mode, report in mode_reports.items()}
    query_ids = list(by_mode["legacy"])
    if set(query_ids) != set(by_mode["answerable"]):
        raise RuntimeError("legacy and answerable reports contain different gold queries")
    return [
        {
            "id": query_id,
            "query": by_mode["legacy"][query_id]["query"],
            "slice": by_mode["legacy"][query_id]["slice"],
            "relevant_claim_ids": by_mode["legacy"][query_id]["relevant_claim_ids"],
            "legacy": {
                key: value
                for key, value in by_mode["legacy"][query_id].items()
                if key
                in {
                    "dense_rank",
                    "dense_cosine",
                    "dense_target_claim_id",
                    "fts_rank",
                    "fts_target_claim_id",
                    "pipeline_rank",
                    "pipeline_target_claim_id",
                    "pipeline_answerability",
                }
            },
            "answerable": {
                key: value
                for key, value in by_mode["answerable"][query_id].items()
                if key
                in {
                    "dense_rank",
                    "dense_cosine",
                    "dense_target_claim_id",
                    "fts_rank",
                    "fts_target_claim_id",
                    "pipeline_rank",
                    "pipeline_target_claim_id",
                    "pipeline_answerability",
                }
            },
        }
        for query_id in query_ids
    ]


def _settings_manifest(settings: Settings, embedder: EmbedderProtocol) -> dict[str, Any]:
    snapshots = {mode: replace(settings, index_text_mode=mode).snapshot() for mode in AB_INDEX_TEXT_MODES}
    differences = sorted(
        key
        for key in set(snapshots["legacy"]) | set(snapshots["answerable"])
        if snapshots["legacy"].get(key) != snapshots["answerable"].get(key)
    )
    if differences != ["index_text_mode"]:
        raise RuntimeError(f"A/B settings differ outside index_text_mode: {differences}")
    return {
        "variable": "index_text_mode",
        "control": "legacy",
        "candidate": "answerable",
        "differing_settings": differences,
        "constant_settings": {key: value for key, value in snapshots["legacy"].items() if key != "index_text_mode"},
        "models": {
            "embedding": {
                "mode": settings.embedder_mode,
                "configured_model": settings.embedding_model,
                "runtime_model": embedder.model,
                "configured_dim": settings.embedding_dim,
                "runtime_dim": embedder.dim,
                "base_url": settings.embedding_base_url,
            },
            "reranker": {
                "mode": settings.reranker_mode,
                "provider": settings.reranker_provider,
                "model": settings.reranker_model,
                "base_url": settings.reranker_base_url,
            },
        },
    }


def run_ab_test(
    snapshot: Path,
    dataset: Path,
    top_k: int,
    settings: Settings | None = None,
    *,
    embedder: EmbedderProtocol | None = None,
    evaluation_runner: EvaluationRunner | None = None,
) -> dict[str, Any]:
    """Run an immutable-snapshot legacy/answerable end-to-end comparison."""
    snapshot = snapshot.resolve()
    dataset = dataset.resolve()
    if top_k < 5:
        raise ValueError("top_k must be at least 5")
    if not snapshot.is_file():
        raise FileNotFoundError(snapshot)
    if not dataset.is_file():
        raise FileNotFoundError(dataset)
    source_hash_before = _file_sha256(snapshot)
    resolved_settings = settings or Settings()
    resolved_embedder = embedder or make_embedder(resolved_settings)
    phase0_runner = _load_phase0_runner()
    evaluation_reference_time = datetime.now(timezone.utc).isoformat()
    runner: EvaluationRunner
    if evaluation_runner is None:

        def default_runner(
            mode_snapshot: Path,
            gold: Path,
            requested_top_k: int,
            mode_settings: Settings | None,
        ) -> dict[str, Any]:
            return cast(
                dict[str, Any],
                phase0_runner.run(
                    mode_snapshot,
                    gold,
                    requested_top_k,
                    mode_settings,
                    reference_time=evaluation_reference_time,
                ),
            )

        runner = default_runner
    else:
        runner = evaluation_runner
    experiment = _settings_manifest(resolved_settings, resolved_embedder)
    report: dict[str, Any] | None = None
    source_hash_after = source_hash_before
    try:
        with tempfile.TemporaryDirectory(prefix="hl-mem-index-text-ab-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            frozen = temporary_root / "frozen.db"
            _freeze_snapshot(snapshot, frozen)
            frozen_hash = _file_sha256(frozen)
            rows = phase0_runner._load_rows(dataset)
            phase0_runner._resolve_ids(frozen, dataset, rows)
            unresolved = [
                str(row["id"]) for row in rows if row["slice"] != "no_answer" and not row["expected_claim_ids"]
            ]
            if unresolved:
                raise ValueError("answerable gold queries lack fixed claim IDs/binding: " + ", ".join(unresolved))
            unique_queries = list(dict.fromkeys(str(row["query"]) for row in rows))
            query_embeddings = dict(
                zip(
                    unique_queries,
                    resolved_embedder.embed_batch(unique_queries),
                    strict=True,
                )
            )
            mode_reports: dict[str, dict[str, Any]] = {}
            for mode in AB_INDEX_TEXT_MODES:
                mode_database = temporary_root / f"{mode}.db"
                shutil.copy2(frozen, mode_database)
                initial_copy_hash = _file_sha256(mode_database)
                if initial_copy_hash != frozen_hash:
                    raise RuntimeError(f"{mode} copy does not match the frozen snapshot")
                mode_settings = replace(
                    resolved_settings,
                    database_path=str(mode_database),
                    index_text_mode=mode,
                )
                projection = _rebuild_projection_copy(
                    mode_database,
                    mode,
                    mode_settings,
                    resolved_embedder,
                )
                projected_copy_hash = _file_sha256(mode_database)
                pipeline_report = runner(
                    mode_database,
                    dataset,
                    top_k,
                    mode_settings,
                )
                mode_reports[mode] = {
                    "artifacts": {
                        "initial_copy_sha256": initial_copy_hash,
                        "projected_copy_sha256": projected_copy_hash,
                    },
                    "projection": projection,
                    "pipeline": pipeline_report,
                    "queries": _channel_diagnostics(
                        mode_database,
                        rows,
                        query_embeddings,
                        pipeline_report,
                        mode_settings,
                        evaluation_reference_time,
                    ),
                }
            dataset_hashes = {
                str(mode_reports[mode]["pipeline"]["artifacts"]["dataset_sha256"]) for mode in AB_INDEX_TEXT_MODES
            }
            if len(dataset_hashes) != 1:
                raise RuntimeError("legacy and answerable runners used different gold datasets")
            canonical_digests = {
                str(mode_reports[mode]["projection"]["canonical_digest"]["after"]) for mode in AB_INDEX_TEXT_MODES
            }
            if len(canonical_digests) != 1:
                raise RuntimeError("legacy and answerable copies differ outside derived projection fields")
            report = {
                "schema_version": 1,
                "source_snapshot": {
                    "path": str(snapshot),
                    "sha256_before": source_hash_before,
                    "frozen_copy_sha256": frozen_hash,
                    "unchanged": True,
                },
                "dataset": {
                    "path": str(dataset),
                    "sha256": dataset_hashes.pop(),
                },
                "experiment": experiment,
                "canonical_digest": {
                    "sha256": canonical_digests.pop(),
                    "unchanged_in_each_arm": True,
                },
                "evaluation_reference_time": evaluation_reference_time,
                "channel_diagnostics": {
                    "scope": "raw_repository",
                    "limit": resolved_settings.recall_vector_scan_limit,
                },
                "top_k": top_k,
                "modes": mode_reports,
                "comparison": {
                    "pipeline_metrics": _metric_comparison(mode_reports),
                    "queries": _query_comparison(mode_reports),
                },
            }
    finally:
        source_hash_after = _file_sha256(snapshot)
        if source_hash_after != source_hash_before:
            raise RuntimeError("source snapshot changed during A/B evaluation")
    assert report is not None
    report["source_snapshot"]["sha256_after"] = source_hash_after
    return report


def _display_rank(value: Any) -> str:
    return str(value) if value is not None else "N/A"


def _display_cosine(value: Any) -> str:
    return f"{float(value):.6f}" if value is not None else "N/A"


def _markdown_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _render_ab_report(report: dict[str, Any]) -> str:
    source = report["source_snapshot"]
    lines = [
        f"Source snapshot SHA-256: `{source['sha256_before']}` (unchanged: {source['unchanged']})",
        "",
        "| Pipeline metric | legacy | answerable | delta |",
        "|---|---:|---:|---:|",
    ]
    for metric, values in report["comparison"]["pipeline_metrics"].items():
        lines.append(f"| {metric} | {values['legacy']:.6f} | {values['answerable']:.6f} | " f"{values['delta']:+.6f} |")
    lines.extend(
        [
            "",
            "| Query | legacy raw dense rank/cosine | legacy raw FTS | legacy pipeline | "
            "answerable raw dense rank/cosine | answerable raw FTS | answerable pipeline |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in report["comparison"]["queries"]:
        legacy = item["legacy"]
        answerable = item["answerable"]
        lines.append(
            f"| {_markdown_text(item['id'])}: {_markdown_text(item['query'])} | "
            f"{_display_rank(legacy['dense_rank'])}/{_display_cosine(legacy['dense_cosine'])} | "
            f"{_display_rank(legacy['fts_rank'])} | {_display_rank(legacy['pipeline_rank'])} | "
            f"{_display_rank(answerable['dense_rank'])}/"
            f"{_display_cosine(answerable['dense_cosine'])} | "
            f"{_display_rank(answerable['fts_rank'])} | "
            f"{_display_rank(answerable['pipeline_rank'])} |"
        )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse immutable-snapshot A/B arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        "--database",
        dest="snapshot",
        type=Path,
        default=Path(os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db")),
        help="Frozen SQLite snapshot; opened read-only and never modified",
    )
    parser.add_argument("--diagnostic-set", type=Path, help="Run the legacy four-mode dense diagnostic")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, help="Optional JSON report path")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.top_k < 5:
        parser.error("--top-k must be at least 5")
    if arguments.report is not None:
        report_path = arguments.report.resolve()
        snapshot_path = arguments.snapshot.resolve()
        protected_paths = {
            snapshot_path,
            Path(f"{snapshot_path}-wal"),
            Path(f"{snapshot_path}-shm"),
            arguments.dataset.resolve(),
        }
        if report_path in protected_paths:
            parser.error("--report must not overwrite --snapshot, its sidecars, or --dataset")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    """Run the end-to-end A/B and print a concise Markdown comparison."""
    arguments = parse_args(argv)
    settings = load_settings(arguments.config, arguments.env_file)
    if arguments.diagnostic_set is not None:
        connection = open_readonly_database(arguments.snapshot)
        try:
            claims = ClaimRepository(connection).list_all()
        finally:
            connection.close()
        diagnostics = _load_diagnostics(arguments.diagnostic_set)
        print(_render_table(compare_index_text_modes(claims, diagnostics, make_embedder(settings))))
        return 0
    report = run_ab_test(
        arguments.snapshot,
        arguments.dataset,
        arguments.top_k,
        settings=settings,
    )
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(_render_ab_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
