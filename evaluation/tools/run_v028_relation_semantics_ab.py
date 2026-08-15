#!/usr/bin/env python
"""Run the frozen v0.28 round-two source-first relation A/B."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools import run_v028_rao_extraction_ab as e3_runner  # noqa: E402
from evaluation.tools import score_c_series_relation_experiment as frozen_scorer  # noqa: E402
from hl_mem.components import (  # noqa: E402
    initialize_process,
    make_embedder,
    make_llm_client,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.evaluation.c_series import sha256_file, write_json_atomic  # noqa: E402
from hl_mem.evaluation.c_series_runtime import (  # noqa: E402
    assert_gold_free,
    recall_visible_case,
    render_packet_context,
)
from hl_mem.evaluation.relation_semantics_ab import (  # noqa: E402
    SOURCE_FIRST_RELATION_OUTPUT_SCHEMA,
    SOURCE_FIRST_RELATION_PROMPT_SHA256,
    SOURCE_FIRST_RELATION_SYSTEM_PROMPT,
    SourceFirstRelationDiscoverer,
    create_experiment_schema,
    load_source_evidence,
    overlay_packet_relations,
    persist_source_annotation,
    validate_source_annotation,
)
from hl_mem.http_utils import find_http_exception, find_http_status_error  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402
from hl_mem.workers.discover_relations import (  # noqa: E402
    RELATION_DISCOVERY_OUTPUT_SCHEMA,
    RELATION_DISCOVERY_SYSTEM_PROMPT,
    LLMRelationDiscoverer,
    discover_relations,
)

OUTPUT_ROOT = ROOT / "var" / "eval"
BASE_CACHE_ROOT = OUTPUT_ROOT / "v028_rao_ab_cache" / "old"
BASE_INPUTS = OUTPUT_ROOT / "v028_rao_ab_inputs_nogold.json"
CACHE_ROOT = OUTPUT_ROOT / "v028_r1_ab_cache"
INPUTS = OUTPUT_ROOT / "v028_r1_ab_inputs_nogold.json"
PREREG = OUTPUT_ROOT / "v028_r1_ab_preregistration.json"
RAW_CALLS = OUTPUT_ROOT / "v028_r1_ab_calls.jsonl"
RAW_RECALL = OUTPUT_ROOT / "v028_r1_ab_recall.jsonl"
REPORT = OUTPUT_ROOT / "v028_r1_ab_report.json"
REPORT_MD = OUTPUT_ROOT / "v028_r1_ab_report.md"

ARMS = ("R0", "R1")
RECALL_ARMS = ("C0", "C4")
PROTOCOL_VERSION = "v028-source-first-relation-ab-v1"
SCORER_VERSION = "answer-entity-packet-v1"
EXPECTED_SOURCE_COUNT = 168
EXPECTED_CALLS = 336
MAX_CALLS = 374
PACKET_TOKEN_BUDGET = 2000
EXPANSION_ELIGIBLE_PREFIX = "cdev:"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _tracked_clean() -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, check=False).returncode == 0
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False).returncode == 0
    return unstaged and staged


def _safe_name(value: str) -> str:
    return e3_runner._safe_name(value)  # noqa: SLF001 - frozen evaluation helper


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl_atomic(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str) + "\n")
    temporary.replace(path)


def paired_task_order(
    sources_by_trajectory: Mapping[str, Sequence[str]],
    *,
    preregistration_id: str,
) -> list[dict[str, str]]:
    """Keep each R0/R1 pair adjacent while randomizing pair and arm order."""
    pairs = [
        (trajectory_id, source_id)
        for trajectory_id, source_ids in sorted(sources_by_trajectory.items())
        for source_id in source_ids
    ]
    pairs.sort(key=lambda item: hashlib.sha256(f"{preregistration_id}|{item[0]}|{item[1]}".encode("utf-8")).digest())
    result: list[dict[str, str]] = []
    for trajectory_id, source_id in pairs:
        arm_order = sorted(
            ARMS,
            key=lambda arm: hashlib.sha256(
                f"{preregistration_id}|{trajectory_id}|{source_id}|{arm}".encode("utf-8")
            ).digest(),
        )
        result.extend({"trajectory_id": trajectory_id, "source_claim_id": source_id, "arm": arm} for arm in arm_order)
    return result


def scrub_relation_discovery_effects(connection: sqlite3.Connection) -> dict[str, int]:
    """Restore the pre-discovery graph without deleting pre-existing conflict cases."""
    connection.row_factory = sqlite3.Row
    relations = int(connection.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0])
    proposals = int(connection.execute("SELECT COUNT(*) FROM relation_proposals").fetchone()[0])
    created_rows = connection.execute(
        "SELECT DISTINCT case_row.id,case_row.left_claim_id,case_row.right_claim_id "
        "FROM conflict_cases case_row JOIN relation_proposals proposal "
        "ON proposal.conflict_case_id=case_row.id "
        "WHERE proposal.status='conflict_created' "
        "AND case_row.created_at=proposal.created_at "
        "AND proposal.decided_at=proposal.created_at "
        "AND case_row.decision='contradicts' "
        "AND case_row.rationale=proposal.rationale "
        "AND ABS(case_row.confidence-proposal.confidence)<0.0000001"
    ).fetchall()
    created_case_ids = [str(row["id"]) for row in created_rows]
    touched_claims = list(
        dict.fromkeys(
            str(claim_id) for row in created_rows for claim_id in (row["left_claim_id"], row["right_claim_id"])
        )
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM relation_proposals")
        connection.execute("DELETE FROM memory_relations")
        if created_case_ids:
            placeholders = ",".join("?" for _ in created_case_ids)
            connection.execute(f"DELETE FROM conflict_cases WHERE id IN ({placeholders})", created_case_ids)
        reactivated = 0
        for claim_id in touched_claims:
            open_case = connection.execute(
                "SELECT 1 FROM conflict_cases WHERE (left_claim_id=? OR right_claim_id=?) "
                "AND status NOT IN ('resolved','rejected') LIMIT 1",
                (claim_id, claim_id),
            ).fetchone()
            if open_case is None:
                cursor = connection.execute(
                    "UPDATE claims SET status='active' WHERE id=? AND status='disputed'",
                    (claim_id,),
                )
                reactivated += int(cursor.rowcount)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "relations": relations,
        "proposals": proposals,
        "created_conflicts": len(created_case_ids),
        "reactivated_claims": reactivated,
    }


def _base_cache_index() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(BASE_CACHE_ROOT.glob("*.manifest.json")):
        manifest = _json(manifest_path)
        trajectory_id = str(manifest["trajectory_id"])
        database = manifest_path.with_name(manifest_path.name.removesuffix(".manifest.json") + ".db")
        if not database.exists():
            raise FileNotFoundError(database)
        if sha256_file(database) != manifest["db_sha256"]:
            raise RuntimeError(f"base cache drift: {trajectory_id}")
        result[trajectory_id] = {"database": database, "manifest": manifest, "manifest_path": manifest_path}
    if len(result) != 28:
        raise RuntimeError(f"expected 28 base trajectory caches, got {len(result)}")
    return result


def _clone_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _jsonable_sql(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    return value


def _table_snapshot(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]
    if not columns:
        return []
    ordering = "id" if "id" in columns else columns[0]
    rows = connection.execute(f"SELECT * FROM {table} ORDER BY {ordering}").fetchall()
    return [{column: _jsonable_sql(row[column]) for column in columns} for row in rows]


def _database_identity_hash(connection: sqlite3.Connection) -> str:
    connection.row_factory = sqlite3.Row
    return _canonical_hash(
        {
            table: _table_snapshot(connection, table)
            for table in (
                "events",
                "claims",
                "evidence_links",
                "conflict_cases",
                "memory_relations",
                "relation_proposals",
                "claim_relation_semantics",
            )
        }
    )


def _claims_identity_hash(database: Path) -> str:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return _canonical_hash(_table_snapshot(connection, "claims"))
    finally:
        connection.close()


def _cache_paths(trajectory_id: str, arm: str) -> tuple[Path, Path]:
    stem = _safe_name(trajectory_id)
    database = CACHE_ROOT / arm / f"{stem}.db"
    return database, database.with_suffix(".manifest.json")


def _cases_by_trajectory(inputs: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in inputs["cases"]:
        result[str(case["trajectory_id"])].append(dict(case))
    return dict(result)


def _compute_frozen_sources(
    inputs: Mapping[str, Any],
    settings: Any,
    base_caches: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Recompute the gold-free C0 source union and freeze the current result."""
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    cases = _cases_by_trajectory(inputs)
    result: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="hlmem-v028-r1-prereg-") as directory:
        temporary_root = Path(directory)
        for trajectory_id, trajectory_cases in sorted(cases.items()):
            temporary = temporary_root / f"{_safe_name(trajectory_id)}.db"
            _clone_database(Path(base_caches[trajectory_id]["database"]), temporary)
            connection = sqlite3.connect(temporary)
            connection.row_factory = sqlite3.Row
            try:
                scrub_relation_discovery_effects(connection)
                create_experiment_schema(connection)
            finally:
                connection.close()
            selected = e3_runner.relation_discovery_seed_ids(
                trajectory_cases,
                settings=settings,
                embedder=embedder,
                reranker=reranker,
                db_path=temporary,
            )
            if selected:
                result[trajectory_id] = selected
    if sum(len(values) for values in result.values()) != EXPECTED_SOURCE_COUNT:
        raise RuntimeError(
            "round-two frozen source count drift: "
            f"expected {EXPECTED_SOURCE_COUNT}, got {sum(len(values) for values in result.values())}"
        )
    return result


def _implementation_snapshot() -> dict[str, str]:
    paths = {
        "runner": Path(__file__),
        "relation_semantics": ROOT / "src" / "hl_mem" / "evaluation" / "relation_semantics_ab.py",
        "relation_discovery": ROOT / "src" / "hl_mem" / "workers" / "discover_relations.py",
        "recall_runtime": ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        "scorer": ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
        "migration_044": ROOT / "src" / "hl_mem" / "storage" / "migrations" / "044_relation_bitemporal.sql",
    }
    return {name: sha256_file(path) for name, path in paths.items()}


def command_preregister() -> int:
    if not _tracked_clean():
        raise RuntimeError("tracked source must be clean before preregistration")
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    base_inputs = _json(BASE_INPUTS)
    assert_gold_free(base_inputs)
    inputs = {**base_inputs, "protocol_version": PROTOCOL_VERSION}
    write_json_atomic(INPUTS, inputs)
    base_caches = _base_cache_index()
    sources = _compute_frozen_sources(inputs, settings, base_caches)
    commit = _git("rev-parse", "HEAD")
    preregistration_id = f"v028-r1-ab-{commit[:12]}"
    tasks = paired_task_order(sources, preregistration_id=preregistration_id)
    if len(tasks) != EXPECTED_CALLS:
        raise RuntimeError(f"expected {EXPECTED_CALLS} paired tasks, got {len(tasks)}")
    prior = _json(e3_runner.PREREG)
    relation_r0 = {
        "prompt_sha256": _canonical_hash(RELATION_DISCOVERY_SYSTEM_PROMPT),
        "schema_sha256": _canonical_hash(RELATION_DISCOVERY_OUTPUT_SCHEMA),
    }
    relation_r1 = {
        "prompt_schema_sha256": SOURCE_FIRST_RELATION_PROMPT_SHA256,
        "prompt_sha256": _canonical_hash(SOURCE_FIRST_RELATION_SYSTEM_PROMPT),
        "schema_sha256": _canonical_hash(SOURCE_FIRST_RELATION_OUTPUT_SCHEMA),
    }
    manifest = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "scorer_version": SCORER_VERSION,
        "preregistration_id": preregistration_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": commit,
        "case_count": 52,
        "trajectory_count": 28,
        "source_count": EXPECTED_SOURCE_COUNT,
        "paired_call_count": EXPECTED_CALLS,
        "repeat_count": 1,
        "preflight": {
            "rejected_attempts": 1,
            "status": 400,
            "reason": "R1 JSON-object prompt omitted an explicit JSON instruction; fixed before frozen run",
        },
        "task_order_sha256": _canonical_hash(tasks),
        "inputs_sha256": sha256_file(INPUTS),
        "sources_by_trajectory": sources,
        "source_ids_sha256": _canonical_hash(sources),
        "source_selection_drift": {
            "prior_manifest_source_count": 170,
            "current_recomputed_source_count": EXPECTED_SOURCE_COUNT,
            "reason": (
                "the prior runner froze only per-trajectory counts, not source IDs; current remote ranking "
                "replay yields 168, so this protocol freezes the reproducible gold-free set without padding"
            ),
        },
        "corpora": prior["corpora"],
        "base_caches": {
            trajectory_id: {
                "db_sha256": sha256_file(Path(item["database"])),
                "manifest_sha256": sha256_file(Path(item["manifest_path"])),
            }
            for trajectory_id, item in sorted(base_caches.items())
        },
        "settings": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "temperature": "provider_default_omitted",
            "structured_mode": settings.llm_structured_mode,
            "per_http_call_attempts": 1,
            "pool_limit": settings.relation_discovery_pool_limit,
            "max_proposals": settings.relation_discovery_max_proposals,
            "auto_apply_confidence": settings.relation_auto_apply_confidence,
            "contradiction_application": "disabled_in_ab_to_preserve_claim_identity",
            "valid_time": "migration-044",
            "source_selector": "gold-free C0 Top-5 union recomputed on scrubbed compact-7field cache",
            "packet_claim_limit": 10,
            "packet_token_budget": PACKET_TOKEN_BUDGET,
        },
        "contracts": {"R0": relation_r0, "R1": relation_r1},
        "implementation_snapshot": _implementation_snapshot(),
        "metrics": [
            "source_annotation_attempt/accepted/discarded_by_reason",
            "source_bounded_acceptance_rate",
            "accepted_source_boundary_precision",
            "source_semantics_without_edge_rate",
            "minimal_span_exactness",
            "packet_claim_preservation",
            "relation_line_omitted_for_budget",
            "proposal_pair_preservation",
            "expansion_eligible_edge_coverage",
            "ordinary_anchor_correct_to_missing",
            "schema_retry_latency",
            "one_round_metrics",
        ],
        "gates": {
            "anchor_floor": 0.929,
            "source_bounded_acceptance_rate": 0.80,
            "accepted_source_boundary_precision": 1.0,
            "exact_rao_absolute": 0.25,
            "exact_rao_net_pp": 0.20,
            "packet_rao_absolute": 0.20,
            "packet_rao_net_pp": 0.15,
            "expansion_eligible_edge_coverage": 0.80,
            "expansion_eligible_edge_net_cases": 2,
            "schema_failure_rate": 0.02,
            "retry_rate": 0.10,
            "input_token_ratio": 1.25,
            "output_token_ratio": 1.35,
            "call_hard_limit": MAX_CALLS,
        },
        "expansion_eligible_rule": "design/dev cdev:* cases with gold answerability=answerable; labels frozen before R1",
        "smoke_case_ids": [
            "cdev:recommendation_execution:01",
            "cdev:reporting_ownership:01",
            "cdev:cross_event_two_hop:01",
        ],
        "sealed_v3": "not_loaded_or_run",
    }
    write_json_atomic(PREREG, manifest)
    print(json.dumps({"preregistration": str(PREREG), "sha256": sha256_file(PREREG), "tasks": len(tasks)}))
    return 0


def _verify_preregistration(settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _json(PREREG)
    inputs = _json(INPUTS)
    if manifest.get("protocol_version") != PROTOCOL_VERSION or manifest.get("scorer_version") != SCORER_VERSION:
        raise RuntimeError("round-two protocol/scorer drift")
    if manifest.get("git_commit") != _git("rev-parse", "HEAD"):
        raise RuntimeError("git commit differs from preregistration")
    if not _tracked_clean():
        raise RuntimeError("tracked source changed after preregistration")
    if manifest.get("inputs_sha256") != sha256_file(INPUTS):
        raise RuntimeError("round-two inputs drift")
    if manifest.get("implementation_snapshot") != _implementation_snapshot():
        raise RuntimeError("round-two implementation drift")
    if manifest["settings"]["model"] != settings.llm_model or manifest["settings"]["base_url"] != settings.llm_base_url:
        raise RuntimeError("round-two LLM settings drift")
    tasks = paired_task_order(manifest["sources_by_trajectory"], preregistration_id=manifest["preregistration_id"])
    if _canonical_hash(tasks) != manifest["task_order_sha256"] or len(tasks) != EXPECTED_CALLS:
        raise RuntimeError("round-two task order drift")
    base_caches = _base_cache_index()
    for trajectory_id, frozen in manifest["base_caches"].items():
        current = base_caches[trajectory_id]
        if frozen["db_sha256"] != sha256_file(Path(current["database"])):
            raise RuntimeError(f"base cache changed after preregistration: {trajectory_id}")
    assert_gold_free(inputs)
    return manifest, inputs


def command_prepare_cache() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, _ = _verify_preregistration(settings)
    base_caches = _base_cache_index()
    prepared = 0
    for trajectory_id, base in sorted(base_caches.items()):
        arm_hashes: dict[str, str] = {}
        for arm in ARMS:
            database, manifest_path = _cache_paths(trajectory_id, arm)
            _clone_database(Path(base["database"]), database)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                scrub_counts = scrub_relation_discovery_effects(connection)
                create_experiment_schema(connection)
                source_ids = prereg["sources_by_trajectory"].get(trajectory_id, [])
                missing = [
                    claim_id
                    for claim_id in source_ids
                    if connection.execute("SELECT 1 FROM claims WHERE id=?", (claim_id,)).fetchone() is None
                ]
                if missing:
                    raise RuntimeError(f"frozen source IDs missing from {trajectory_id}: {missing}")
                identity = _database_identity_hash(connection)
            finally:
                connection.close()
            arm_hashes[arm] = identity
            write_json_atomic(
                manifest_path,
                {
                    "schema_version": 1,
                    "protocol_version": PROTOCOL_VERSION,
                    "preregistration_sha256": sha256_file(PREREG),
                    "trajectory_id": trajectory_id,
                    "arm": arm,
                    "base_db_sha256": sha256_file(Path(base["database"])),
                    "clean_identity_sha256": identity,
                    "source_ids_sha256": _canonical_hash(prereg["sources_by_trajectory"].get(trajectory_id, [])),
                    "source_count": len(prereg["sources_by_trajectory"].get(trajectory_id, [])),
                    "scrub_counts": scrub_counts,
                },
            )
            prepared += 1
        if arm_hashes["R0"] != arm_hashes["R1"]:
            raise RuntimeError(f"paired clean cache mismatch: {trajectory_id}")
    print(json.dumps({"prepared": prepared, "cache_root": str(CACHE_ROOT)}))
    return 0


def _cache_index(prereg: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for trajectory_id in prereg["base_caches"]:
        paired_hashes: dict[str, str] = {}
        for arm in ARMS:
            database, manifest_path = _cache_paths(trajectory_id, arm)
            if not database.exists() or not manifest_path.exists():
                raise FileNotFoundError(f"round-two cache missing: {trajectory_id}/{arm}")
            manifest = _json(manifest_path)
            if manifest.get("preregistration_sha256") != sha256_file(PREREG):
                raise RuntimeError(f"round-two cache manifest drift: {trajectory_id}/{arm}")
            paired_hashes[arm] = str(manifest["clean_identity_sha256"])
            result[(trajectory_id, arm)] = {"database": database, "manifest": manifest}
        if paired_hashes["R0"] != paired_hashes["R1"]:
            raise RuntimeError(f"round-two paired cache identity mismatch: {trajectory_id}")
    return result


def _latest_span(connection: sqlite3.Connection, operation: str, before_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM llm_call_spans WHERE operation=? AND id>? ORDER BY id DESC LIMIT 1",
        (operation, before_id),
    ).fetchone()
    if row is None:
        return {"status": "missing", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "latency_ms": 0.0}
    return dict(row)


def _retryable(error: BaseException) -> bool:
    status_error = find_http_status_error(error)
    if status_error is not None:
        return status_error.response.status_code == 429 or status_error.response.status_code >= 500
    try:
        import httpx

        return find_http_exception(error, (httpx.TimeoutException, httpx.ConnectError)) is not None
    except ImportError:
        return False


def command_run() -> int:
    if RAW_CALLS.exists():
        raise RuntimeError(f"refusing duplicate paid run; archive or explicitly remove {RAW_CALLS}")
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, _ = _verify_preregistration(settings)
    caches = _cache_index(prereg)
    runtime = dataclasses.replace(
        settings,
        llm_max_attempts=1,
        relation_discovery_mode="auto",
        query_expansion_mode="off",
    )
    runtime.validate()
    initialize_process(runtime)
    tasks = paired_task_order(prereg["sources_by_trajectory"], preregistration_id=prereg["preregistration_id"])
    records: list[dict[str, Any]] = []
    total_attempts = int(prereg.get("preflight", {}).get("rejected_attempts", 0))
    for index, task in enumerate(tasks, start=1):
        trajectory_id = task["trajectory_id"]
        source_id = task["source_claim_id"]
        arm = task["arm"]
        database_path = Path(caches[(trajectory_id, arm)]["database"])
        per_task_attempt = 0
        while True:
            if total_attempts >= MAX_CALLS:
                raise RuntimeError(f"round-two call hard limit exceeded: {MAX_CALLS}")
            total_attempts += 1
            per_task_attempt += 1
            database = Database(database_path, settings=runtime)
            connection = database.open()
            operation = f"v028_r1_ab_{arm}"
            before_span = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM llm_call_spans").fetchone()[0])
            started = time.perf_counter()
            try:
                client = make_llm_client(runtime, connection, operation=operation)
                if arm == "R0":
                    discoverer: Any = LLMRelationDiscoverer(client)
                else:
                    discoverer = SourceFirstRelationDiscoverer(
                        client,
                        evidence_loader=lambda claim_id, connection=connection: load_source_evidence(
                            connection, claim_id
                        ),
                    )
                counts = discover_relations(
                    connection,
                    discoverer,
                    source_id,
                    mode="auto",
                    pool_limit=runtime.relation_discovery_pool_limit,
                    max_proposals=runtime.relation_discovery_max_proposals,
                    auto_apply_confidence=runtime.relation_auto_apply_confidence,
                    conflict_confidence=1.1,
                )
                validation_reason = "not_applicable"
                annotation_stored = False
                annotation_attempted = False
                relations_empty = counts["proposals"] == 0
                if arm == "R1":
                    annotation_attempted = discoverer.last_source_semantics is not None
                    validation = validate_source_annotation(
                        connection,
                        source_id,
                        discoverer.last_source_semantics,
                    )
                    validation_reason = validation.reason
                    relations_empty = bool(discoverer.last_relations_empty)
                    if validation.annotation is not None:
                        persist_source_annotation(
                            connection,
                            validation.annotation,
                            model=client.model,
                            prompt_sha256=SOURCE_FIRST_RELATION_PROMPT_SHA256,
                        )
                        annotation_stored = True
                span = _latest_span(connection, operation, before_span)
                records.append(
                    {
                        "task_index": index,
                        "trajectory_id": trajectory_id,
                        "source_claim_id": source_id,
                        "arm": arm,
                        "status": "success",
                        "attempts": per_task_attempt,
                        "counts": counts,
                        "annotation_attempted": annotation_attempted,
                        "annotation_stored": annotation_stored,
                        "validation_reason": validation_reason,
                        "relations_empty": relations_empty,
                        "input_tokens": int(span.get("input_tokens") or 0),
                        "output_tokens": int(span.get("output_tokens") or 0),
                        "total_tokens": int(span.get("total_tokens") or 0),
                        "latency_ms": float(span.get("latency_ms") or (time.perf_counter() - started) * 1000),
                    }
                )
                break
            except Exception as error:
                span = _latest_span(connection, operation, before_span)
                if _retryable(error) and per_task_attempt < 3 and total_attempts < MAX_CALLS:
                    database.close()
                    time.sleep(min(30.0, float(2**per_task_attempt)))
                    continue
                if isinstance(error, (json.JSONDecodeError, KeyError, TypeError, ValueError)) and not _retryable(error):
                    records.append(
                        {
                            "task_index": index,
                            "trajectory_id": trajectory_id,
                            "source_claim_id": source_id,
                            "arm": arm,
                            "status": "schema_failure",
                            "attempts": per_task_attempt,
                            "counts": {},
                            "annotation_attempted": False,
                            "annotation_stored": False,
                            "validation_reason": "schema_failure",
                            "relations_empty": True,
                            "input_tokens": int(span.get("input_tokens") or 0),
                            "output_tokens": int(span.get("output_tokens") or 0),
                            "total_tokens": int(span.get("total_tokens") or 0),
                            "latency_ms": float(span.get("latency_ms") or (time.perf_counter() - started) * 1000),
                            "error_class": type(error).__name__,
                        }
                    )
                    break
                raise
            finally:
                database.close()
        if index % 10 == 0 or index == len(tasks):
            print(json.dumps({"completed": index, "tasks": len(tasks), "attempts": total_attempts}), flush=True)
    if len(records) != EXPECTED_CALLS:
        raise RuntimeError(f"round-two call record count mismatch: {len(records)}")
    _write_jsonl_atomic(RAW_CALLS, records)
    print(json.dumps({"calls": len(records), "attempts": total_attempts, "raw": str(RAW_CALLS)}))
    return 0


def _text_only_packet(packet: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in packet:
        item = {key: value for key, value in raw.items() if key not in {"role", "action", "object"}}
        text = str(item.get("text") or "")
        item["rendered_text"] = text
        item["token_count"] = max(1, (len(text) + 1) // 2)
        result.append(item)
    return result


def command_recall() -> int:
    if RAW_RECALL.exists():
        raise RuntimeError(f"refusing duplicate recall; archive or explicitly remove {RAW_RECALL}")
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, inputs = _verify_preregistration(settings)
    caches = _cache_index(prereg)
    runtime = dataclasses.replace(settings, relation_discovery_mode="auto", query_expansion_mode="off")
    runtime.validate()
    initialize_process(runtime)
    embedder = make_embedder(runtime)
    reranker = make_reranker(runtime)
    rows: list[dict[str, Any]] = []
    for case in inputs["cases"]:
        for arm in ARMS:
            database_path = Path(caches[(str(case["trajectory_id"]), arm)]["database"])
            for recall_arm in RECALL_ARMS:
                recalled = recall_visible_case(
                    case,
                    runtime,
                    embedder,
                    reranker,
                    db_path=database_path,
                    arm_id=recall_arm,
                )
                baseline = _text_only_packet(recalled.packet)
                overlay_metrics: dict[str, Any] = {
                    "available": 0,
                    "rendered": 0,
                    "omitted_for_budget": 0,
                    "token_overhead": 0,
                    "claim_ids_preserved": True,
                }
                packet = baseline
                if arm == "R1":
                    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
                    connection.row_factory = sqlite3.Row
                    try:
                        packet, overlay_metrics = overlay_packet_relations(
                            connection,
                            baseline,
                            token_budget=PACKET_TOKEN_BUDGET,
                        )
                    finally:
                        connection.close()
                rows.append(
                    {
                        "case_id": str(case["case_id"]),
                        "dataset": str(case["dataset"]),
                        "category": str(case["category"]),
                        "trajectory_id": str(case["trajectory_id"]),
                        "arm": arm,
                        "recall_arm": recall_arm,
                        "packet": packet,
                        "baseline_packet_sha256": _canonical_hash(baseline),
                        "baseline_claim_ids": [str(item["claim_id"]) for item in baseline],
                        "overlay_metrics": overlay_metrics,
                        "answerability": recalled.answerability,
                        "relation_paths": list(recalled.relation_paths),
                        "recall_latency_seconds": recalled.recall_latency_seconds,
                        "source_cache_identity": str(database_path.resolve()),
                        "source_cache_sha256": sha256_file(database_path),
                    }
                )
    if len(rows) != 52 * len(ARMS) * len(RECALL_ARMS):
        raise RuntimeError(f"round-two recall row count mismatch: {len(rows)}")
    assert_gold_free(rows)
    _write_jsonl_atomic(RAW_RECALL, rows)
    print(json.dumps({"rows": len(rows), "raw": str(RAW_RECALL)}))
    return 0


def _nfc(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _mean(values: Sequence[float]) -> float:
    return fmean(values) if values else 0.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


def _annotation_triples(database: Path) -> set[tuple[str, str, str]]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {
            (_nfc(row["subject_entity_id"]), _nfc(row["action"]), _nfc(row["object"]))
            for row in connection.execute(
                "SELECT claim.subject_entity_id,semantic.action,semantic.object "
                "FROM claim_relation_semantics semantic JOIN claims claim ON claim.id=semantic.claim_id"
            )
        }
    finally:
        connection.close()


def _expected_triples(gold: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (_nfc(item["role"]), _nfc(item["action"]), _nfc(item["object"]))
        for item in gold.get("role_action_object") or []
    }


def _visible_score(packet: Sequence[Mapping[str, Any]], gold_item: Mapping[str, Any]) -> dict[str, Any]:
    return frozen_scorer.score_visible_case("", packet, gold_item["gold"])


def _arm_metrics(
    arm: str,
    rows: Mapping[tuple[str, str, str], Mapping[str, Any]],
    call_rows: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    gold: Mapping[str, Mapping[str, Any]],
    caches: Mapping[tuple[str, str], Mapping[str, Any]],
    prereg: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    trajectory_ids = sorted({str(case["trajectory_id"]) for case in cases})
    databases = [Path(caches[(trajectory_id, arm)]["database"]) for trajectory_id in trajectory_ids]
    events = claims = canonical_mismatches = canonical_total = 0
    actual_by_trajectory: dict[str, set[tuple[str, str, str]]] = {}
    cache_files: dict[str, str] = {}
    for trajectory_id, database in zip(trajectory_ids, databases, strict=True):
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            events += int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            claims += int(connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        finally:
            connection.close()
        mismatch, total = e3_runner._canonical_slot_mismatches(database)  # noqa: SLF001
        canonical_mismatches += mismatch
        canonical_total += total
        actual_by_trajectory[trajectory_id] = _annotation_triples(database)
        cache_files[str(database.resolve())] = sha256_file(database)

    exact_scores: list[float] = []
    required_cases = relation_covered = 0
    expansion_total = expansion_covered = 0
    per_recall: dict[str, dict[str, Any]] = {}
    anchor_by_case: dict[tuple[str, str], bool] = {}
    provenance_manifest = {"cache_files": cache_files, "corpora": prereg["corpora"]}
    for recall_arm in RECALL_ARMS:
        entity_scores: list[float] = []
        packet_rao: list[float] = []
        anchor_scores: list[float] = []
        violations: Counter[str] = Counter()
        for case in cases:
            case_id = str(case["case_id"])
            row = rows[(case_id, arm, recall_arm)]
            packet = row["packet"]
            gold_item = gold[case_id]
            scored = _visible_score(packet, gold_item)
            if scored.get("entity_coverage_at_5") is not None:
                entity_scores.append(float(scored["entity_coverage_at_5"]))
            if gold_item["gold"].get("role_action_object"):
                packet_rao.append(float(bool(scored["packet_rao_match"])))
            if gold_item["legacy_anchors"]:
                correct = bool(
                    importlib.import_module("tests.eval.chinese_e2e").score_answer(
                        render_packet_context(packet),
                        gold_item["legacy_anchors"],
                        gold_item["accepted_rubrics"],
                    )["answer_correct"]
                )
                anchor_by_case[(case_id, recall_arm)] = correct
                anchor_scores.append(float(correct))
            violations["forbidden"] += int(bool(scored["negative_violation"]))
            runtime_case = {
                **case,
                "source_cache_identity": row["source_cache_identity"],
                "source_cache_sha256": row["source_cache_sha256"],
            }
            audit = frozen_scorer.audit_evidence_provenance(packet, runtime_case, provenance_manifest)
            violations["modality"] += int(bool(audit["modality"]))
            violations["provenance"] += int(bool(audit["provenance"]))
        per_recall[recall_arm] = {
            "entity_coverage_at_5": _mean(entity_scores),
            "packet_rao_completeness": _mean(packet_rao),
            "legacy_anchor_coverage": _mean(anchor_scores),
            "legacy_anchor_cases": len(anchor_scores),
            "forbidden_violations": violations["forbidden"],
            "modality_violations": violations["modality"],
            "provenance_violations": violations["provenance"],
        }

    for case in cases:
        case_id = str(case["case_id"])
        expected = _expected_triples(gold[case_id]["gold"])
        if not expected:
            continue
        required_cases += 1
        actual = actual_by_trajectory[str(case["trajectory_id"])]
        exact_scores.append(float(expected.issubset(actual)))
        c4_row = rows[(case_id, arm, "C4")]
        if c4_row.get("relation_paths"):
            relation_covered += 1
        if case_id.startswith(EXPANSION_ELIGIBLE_PREFIX) and gold[case_id]["gold"].get("answerability") == "answerable":
            expansion_total += 1
            if c4_row.get("relation_paths"):
                expansion_covered += 1

    arm_calls = [row for row in call_rows if row["arm"] == arm]
    attempted = sum(int(bool(row.get("annotation_attempted"))) for row in arm_calls)
    accepted = sum(int(row.get("validation_reason") == "accepted") for row in arm_calls)
    discarded = sum(
        int(bool(row.get("annotation_attempted")) and row.get("validation_reason") != "accepted") for row in arm_calls
    )
    stored = sum(int(bool(row.get("annotation_stored"))) for row in arm_calls)
    without_edge = sum(
        int(row.get("validation_reason") == "accepted" and bool(row.get("relations_empty"))) for row in arm_calls
    )
    schema_failures = sum(int(row.get("status") == "schema_failure") for row in arm_calls)
    retries = sum(max(0, int(row.get("attempts", 1)) - 1) for row in arm_calls)
    call_latencies = [float(row.get("latency_ms") or 0.0) for row in arm_calls]
    c4 = per_recall["C4"]
    leakage = int(bool(frozen_scorer.audit_leakage(inputs))) + int(
        bool(frozen_scorer.audit_leakage([row for key, row in rows.items() if key[1] == arm]))
    )
    reason_counts = Counter(str(row.get("validation_reason")) for row in arm_calls)
    return {
        "claim_yield_per_event": claims / events if events else 0.0,
        "nonrelation_claim_yield_per_event": claims / events if events else 0.0,
        "events": events,
        "claims_stored": claims,
        "canonical_slot_mismatch_rate": canonical_mismatches / canonical_total if canonical_total else 0.0,
        "canonical_slot_mismatches": canonical_mismatches,
        "canonical_slot_cases": canonical_total,
        "source_annotation_attempted": attempted,
        "source_annotation_accepted": accepted,
        "source_annotation_discarded": discarded,
        "source_annotation_reason_counts": dict(sorted(reason_counts.items())),
        "source_bounded_acceptance_rate": accepted / (accepted + discarded) if accepted + discarded else 0.0,
        "accepted_source_boundary_precision": stored / accepted if accepted else (1.0 if arm == "R0" else 0.0),
        "source_semantics_without_edge_rate": without_edge / accepted if accepted else 0.0,
        "exact_rao_rate": _mean(exact_scores),
        "exact_rao_cases": len(exact_scores),
        "packet_rao_completeness": c4["packet_rao_completeness"],
        "entity_coverage_at_5": c4["entity_coverage_at_5"],
        "legacy_anchor_coverage": {
            recall_arm: per_recall[recall_arm]["legacy_anchor_coverage"] for recall_arm in RECALL_ARMS
        },
        "forbidden_violations": sum(per_recall[arm_id]["forbidden_violations"] for arm_id in RECALL_ARMS),
        "modality_violations": sum(per_recall[arm_id]["modality_violations"] for arm_id in RECALL_ARMS),
        "provenance_violations": sum(per_recall[arm_id]["provenance_violations"] for arm_id in RECALL_ARMS),
        "leakage_violations": leakage,
        "proposal_visible_edge_coverage": relation_covered / required_cases if required_cases else 0.0,
        "relation_required_cases": required_cases,
        "relation_covered_cases": relation_covered,
        "expansion_eligible_edge_coverage": expansion_covered / expansion_total if expansion_total else 0.0,
        "expansion_eligible_edge_cases": expansion_covered,
        "expansion_eligible_cases": expansion_total,
        "proposal_schema_failure_rate": schema_failures / len(arm_calls) if arm_calls else 0.0,
        "retry_rate": retries / len(arm_calls) if arm_calls else 0.0,
        "logical_tasks": len(arm_calls),
        "http_attempts": sum(int(row.get("attempts", 1)) for row in arm_calls),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in arm_calls),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in arm_calls),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in arm_calls),
        "latency_ms_p50": _percentile(call_latencies, 0.50),
        "latency_ms_p95": _percentile(call_latencies, 0.95),
        "recall_arms": per_recall,
        "anchor_by_case": {f"{case_id}|{recall_arm}": value for (case_id, recall_arm), value in anchor_by_case.items()},
    }


def evaluate_three_layer_gates(
    r0: Mapping[str, Any],
    r1: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    safety = all(
        int(r1[key]) == 0
        for key in ("forbidden_violations", "modality_violations", "provenance_violations", "leakage_violations")
    )
    isolation_checks = [
        {"id": "cache_hashes_identical", "passed": bool(diagnostics["cache_hashes_identical"])},
        {"id": "claim_identity_identical", "passed": bool(diagnostics["claim_identity_identical"])},
        {
            "id": "claim_yield_identical",
            "passed": float(r1["claim_yield_per_event"]) == float(r0["claim_yield_per_event"]),
        },
        {
            "id": "nonrelation_yield_identical",
            "passed": float(r1["nonrelation_claim_yield_per_event"]) == float(r0["nonrelation_claim_yield_per_event"]),
        },
        {"id": "safety_zero", "passed": safety},
        {"id": "c0_packet_claim_preservation", "passed": bool(diagnostics["c0_packet_claim_preservation"])},
        {"id": "baseline_claim_displacements_zero", "passed": int(diagnostics["baseline_claim_displacements"]) == 0},
    ]
    r0_anchors = r0["legacy_anchor_coverage"]
    r1_anchors = r1["legacy_anchor_coverage"]
    basic_checks = [
        {
            "id": "legacy_anchor_floor",
            "passed": all(float(r1_anchors[arm]) >= 0.929 for arm in RECALL_ARMS),
        },
        {
            "id": "legacy_anchor_no_regression",
            "passed": all(float(r1_anchors[arm]) >= float(r0_anchors[arm]) for arm in RECALL_ARMS),
        },
        {
            "id": "ordinary_anchor_zero_regression",
            "passed": int(diagnostics["ordinary_anchor_correct_to_missing"]) == 0,
        },
        {
            "id": "entity_coverage_no_regression",
            "passed": float(r1["entity_coverage_at_5"]) >= float(r0["entity_coverage_at_5"]),
        },
        {
            "id": "canonical_slot_no_regression",
            "passed": float(r1["canonical_slot_mismatch_rate"]) <= float(r0["canonical_slot_mismatch_rate"]),
        },
        {
            "id": "proposal_visible_edge_no_regression",
            "passed": float(r1["proposal_visible_edge_coverage"]) >= float(r0["proposal_visible_edge_coverage"]),
        },
        {"id": "proposal_schema_failure_rate", "passed": float(r1["proposal_schema_failure_rate"]) <= 0.02},
        {"id": "retry_rate", "passed": float(r1["retry_rate"]) <= 0.10},
    ]
    semantics_checks = [
        {"id": "source_bounded_acceptance", "passed": float(r1["source_bounded_acceptance_rate"]) >= 0.80},
        {
            "id": "source_boundary_precision",
            "passed": float(r1["accepted_source_boundary_precision"]) == 1.0,
        },
        {
            "id": "exact_rao",
            "passed": float(r1["exact_rao_rate"]) >= 0.25
            and float(r1["exact_rao_rate"]) - float(r0["exact_rao_rate"]) >= 0.20,
        },
        {
            "id": "packet_rao",
            "passed": float(r1["packet_rao_completeness"]) >= 0.20
            and float(r1["packet_rao_completeness"]) - float(r0["packet_rao_completeness"]) >= 0.15,
        },
        {
            "id": "source_semantics_without_edge",
            "passed": float(r1["source_semantics_without_edge_rate"]) > 0.0,
        },
    ]
    c4_checks = [
        {
            "id": "expansion_edge_coverage",
            "passed": float(r1["expansion_eligible_edge_coverage"]) >= 0.80
            and int(r1["expansion_eligible_edge_cases"]) - int(r0["expansion_eligible_edge_cases"]) >= 2,
        },
        {"id": "c0_c4_packet_smoke", "passed": bool(diagnostics["packet_smoke_passed"])},
        {"id": "input_token_ratio", "passed": float(diagnostics["input_token_ratio"]) <= 1.25},
        {"id": "output_token_ratio", "passed": float(diagnostics["output_token_ratio"]) <= 1.35},
        {"id": "call_hard_limit", "passed": int(diagnostics["logical_calls"]) <= MAX_CALLS},
    ]
    relation_checks = [*semantics_checks, *c4_checks]
    layers = [
        {"id": "isolation_safety", "checks": isolation_checks},
        {"id": "ordinary_baseline", "checks": basic_checks},
        {"id": "relation_effectiveness", "checks": relation_checks},
    ]
    for layer in layers:
        layer["passed"] = all(bool(check["passed"]) for check in layer["checks"])
    first_two = all(bool(layer["passed"]) for layer in layers[:2])
    semantics_passed = first_two and all(bool(check["passed"]) for check in semantics_checks)
    sealed_eligible = semantics_passed and all(bool(check["passed"]) for check in c4_checks)
    return {
        "passed": all(bool(layer["passed"]) for layer in layers),
        "semantics_gate_passed": semantics_passed,
        "sealed_v3_eligible": sealed_eligible,
        "layers": layers,
    }


def command_score() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, inputs = _verify_preregistration(settings)
    caches = _cache_index(prereg)
    raw_rows = _read_jsonl(RAW_RECALL)
    call_rows = _read_jsonl(RAW_CALLS)
    if len(raw_rows) != 52 * len(ARMS) * len(RECALL_ARMS) or len(call_rows) != EXPECTED_CALLS:
        raise RuntimeError("round-two raw artifact count mismatch")
    assert_gold_free(raw_rows)
    rows = {(str(row["case_id"]), str(row["arm"]), str(row["recall_arm"])): row for row in raw_rows}
    if len(rows) != len(raw_rows):
        raise RuntimeError("duplicate round-two recall rows")
    gold = e3_runner._gold_index()  # noqa: SLF001
    metrics = {arm: _arm_metrics(arm, rows, call_rows, inputs["cases"], gold, caches, prereg, inputs) for arm in ARMS}
    cache_hashes_identical = all(
        caches[(trajectory_id, "R0")]["manifest"]["clean_identity_sha256"]
        == caches[(trajectory_id, "R1")]["manifest"]["clean_identity_sha256"]
        for trajectory_id in prereg["base_caches"]
    )
    claim_identity_identical = all(
        _claims_identity_hash(Path(caches[(trajectory_id, "R0")]["database"]))
        == _claims_identity_hash(Path(caches[(trajectory_id, "R1")]["database"]))
        for trajectory_id in prereg["base_caches"]
    )
    c0_preserved = all(
        bool(row["overlay_metrics"]["claim_ids_preserved"])
        for key, row in rows.items()
        if key[1] == "R1" and key[2] == "C0"
    )
    displacements = sum(
        int(not bool(row["overlay_metrics"]["claim_ids_preserved"])) for key, row in rows.items() if key[1] == "R1"
    )
    r0_anchor = metrics["R0"].pop("anchor_by_case")
    r1_anchor = metrics["R1"].pop("anchor_by_case")
    correct_to_missing = sum(int(bool(value) and not bool(r1_anchor.get(key))) for key, value in r0_anchor.items())
    smoke_details: dict[str, Any] = {}
    for case_id in prereg["smoke_case_ids"]:
        c0 = rows[(case_id, "R1", "C0")]["packet"]
        c4 = rows[(case_id, "R1", "C4")]["packet"]
        smoke_details[case_id] = {
            "C0": _canonical_hash(c0),
            "C4": _canonical_hash(c4),
            "different": _canonical_hash(c0) != _canonical_hash(c4),
        }
    input_ratio = (
        metrics["R1"]["input_tokens"] / metrics["R0"]["input_tokens"] if metrics["R0"]["input_tokens"] else math.inf
    )
    output_ratio = (
        metrics["R1"]["output_tokens"] / metrics["R0"]["output_tokens"] if metrics["R0"]["output_tokens"] else math.inf
    )
    diagnostics = {
        "cache_hashes_identical": cache_hashes_identical,
        "claim_identity_identical": claim_identity_identical,
        "c0_packet_claim_preservation": c0_preserved,
        "baseline_claim_displacements": displacements,
        "ordinary_anchor_correct_to_missing": correct_to_missing,
        "packet_smoke_passed": all(bool(item["different"]) for item in smoke_details.values()),
        "packet_smoke": smoke_details,
        "input_token_ratio": input_ratio,
        "output_token_ratio": output_ratio,
        "logical_calls": int(prereg.get("preflight", {}).get("rejected_attempts", 0))
        + sum(int(row.get("attempts", 1)) for row in call_rows),
        "proposal_pair_preservation": {
            "R0_proposals": sum(
                int(row.get("counts", {}).get("proposals", 0)) for row in call_rows if row["arm"] == "R0"
            ),
            "R1_proposals": sum(
                int(row.get("counts", {}).get("proposals", 0)) for row in call_rows if row["arm"] == "R1"
            ),
            "R0_applied": sum(int(row.get("counts", {}).get("applied", 0)) for row in call_rows if row["arm"] == "R0"),
            "R1_applied": sum(int(row.get("counts", {}).get("applied", 0)) for row in call_rows if row["arm"] == "R1"),
        },
        "relation_line_omitted_for_budget": sum(
            int(row["overlay_metrics"].get("omitted_for_budget", 0)) for key, row in rows.items() if key[1] == "R1"
        ),
        "relation_token_overhead": sum(
            int(row["overlay_metrics"].get("token_overhead", 0)) for key, row in rows.items() if key[1] == "R1"
        ),
    }
    gates = evaluate_three_layer_gates(metrics["R0"], metrics["R1"], diagnostics)
    report = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "scorer_version": SCORER_VERSION,
        "preregistration_sha256": sha256_file(PREREG),
        "raw_calls_sha256": sha256_file(RAW_CALLS),
        "raw_recall_sha256": sha256_file(RAW_RECALL),
        "metrics": metrics,
        "diagnostics": diagnostics,
        "gates": gates,
        "sealed_v3": "not_run",
    }
    write_json_atomic(REPORT, report)
    lines = [
        "# v0.28 source-first relation A/B",
        "",
        "| arm | source bounded | exact RAO | packet RAO | entity@5 | anchors C0/C4 | edge coverage | expansion edge | calls | tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        item = metrics[arm]
        lines.append(
            f"| {arm} | {item['source_bounded_acceptance_rate']:.4f} | {item['exact_rao_rate']:.4f} | "
            f"{item['packet_rao_completeness']:.4f} | {item['entity_coverage_at_5']:.4f} | "
            f"{item['legacy_anchor_coverage']['C0']:.4f}/{item['legacy_anchor_coverage']['C4']:.4f} | "
            f"{item['proposal_visible_edge_coverage']:.4f} | {item['expansion_eligible_edge_coverage']:.4f} | "
            f"{item['http_attempts']} | {item['total_tokens']} |"
        )
    lines.extend(
        [
            "",
            f"- semantics gate: **{'PASS' if gates['semantics_gate_passed'] else 'FAIL'}**",
            f"- sealed v3 eligible: **{'YES' if gates['sealed_v3_eligible'] else 'NO'}**",
        ]
    )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(REPORT),
                "preregistration_sha256": sha256_file(PREREG),
                "passed": gates["passed"],
                "sealed_v3_eligible": gates["sealed_v3_eligible"],
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preregister", "prepare-cache", "run", "recall", "score"))
    args = parser.parse_args()
    if args.command == "preregister":
        return command_preregister()
    if args.command == "prepare-cache":
        return command_prepare_cache()
    if args.command == "run":
        return command_run()
    if args.command == "recall":
        return command_recall()
    return command_score()


if __name__ == "__main__":
    raise SystemExit(main())
