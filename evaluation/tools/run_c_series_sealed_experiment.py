#!/usr/bin/env python
"""Prepare, preregister, and run the authorized C-series sealed 2x2 matrix.

The live command reads only gold-free inputs and frozen packet snapshots.  The
sealed payload is opened by cache preparation/preregistration and by the
separate offline scorer, never by the live reader process.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools import run_c_series_relation_experiment as base  # noqa: E402
from hl_mem.application.ingest import IngestService  # noqa: E402
from hl_mem.components import (  # noqa: E402
    initialize_process,
    make_embedder,
    make_extractor,
    make_relation_discoverer,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.evaluation.c_series import (  # noqa: E402
    PROTOCOL_VERSION,
    arm_spec,
    case_seed,
    is_retryable_error,
    sha256_file,
    write_json_atomic,
)
from hl_mem.evaluation.c_series_runtime import (  # noqa: E402
    assert_gold_free,
    recall_visible_case,
)
from hl_mem.ingest.llm_extractor import LLM_EXTRACTOR_VERSION  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402
from hl_mem.workers.discover_relations import discover_relations  # noqa: E402
from tests.eval.relation_chain_holdout import (  # noqa: E402
    load_holdout_manifest,
    load_sealed_holdout,
    resolve_holdout_path,
)

OUTPUT_ROOT = ROOT / "var" / "eval"


@dataclasses.dataclass(frozen=True)
class SealedSuitePaths:
    version: str
    case_prefix: str
    holdout_manifest: Path
    cache_root: Path
    prereg: Path
    inputs: Path
    packets: Path
    raw: Path
    report: Path
    report_md: Path


def suite_paths(version: str) -> SealedSuitePaths:
    if version == "v1":
        suffix = ""
        manifest_name = "relation_chain_holdout_manifest.json"
        case_prefix = "rc-holdout-v1-"
    elif version == "v2":
        suffix = "_v2"
        manifest_name = "relation_chain_holdout_v2_manifest.json"
        case_prefix = "rc-holdout-v2-"
    else:
        raise ValueError(f"unknown sealed suite: {version}")
    return SealedSuitePaths(
        version=version,
        case_prefix=case_prefix,
        holdout_manifest=ROOT / "tests" / "eval" / "fixtures" / manifest_name,
        cache_root=OUTPUT_ROOT / f"c_series_sealed_cache{suffix}",
        prereg=OUTPUT_ROOT / f"c_series_sealed_preregistration{suffix}.json",
        inputs=OUTPUT_ROOT / f"c_series_sealed_inputs_nogold{suffix}.json",
        packets=OUTPUT_ROOT / f"c_series_sealed_packets{suffix}.json",
        raw=OUTPUT_ROOT / f"c_series_sealed_raw{suffix}.jsonl",
        report=OUTPUT_ROOT / f"c_series_sealed_report{suffix}.json",
        report_md=OUTPUT_ROOT / f"c_series_sealed_report{suffix}.md",
    )


CURRENT_SUITE = suite_paths("v1")
HOLDOUT_MANIFEST = CURRENT_SUITE.holdout_manifest
CACHE_ROOT = CURRENT_SUITE.cache_root
PREREG = CURRENT_SUITE.prereg
INPUTS = CURRENT_SUITE.inputs
PACKETS = CURRENT_SUITE.packets
RAW = CURRENT_SUITE.raw
REPORT = CURRENT_SUITE.report
REPORT_MD = CURRENT_SUITE.report_md


def configure_suite(version: str) -> SealedSuitePaths:
    global CACHE_ROOT, CURRENT_SUITE, HOLDOUT_MANIFEST, INPUTS, PACKETS, PREREG, RAW, REPORT, REPORT_MD
    CURRENT_SUITE = suite_paths(version)
    HOLDOUT_MANIFEST = CURRENT_SUITE.holdout_manifest
    CACHE_ROOT = CURRENT_SUITE.cache_root
    PREREG = CURRENT_SUITE.prereg
    INPUTS = CURRENT_SUITE.inputs
    PACKETS = CURRENT_SUITE.packets
    RAW = CURRENT_SUITE.raw
    REPORT = CURRENT_SUITE.report
    REPORT_MD = CURRENT_SUITE.report_md
    return CURRENT_SUITE


ARMS = ("C0", "C4")
READERS = ("qwen", "glm")
REPEATS = 3
GLM_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
GLM_MODEL = "glm-5.3"
GLM_KEY_ENV = "C_SERIES_ZHIPU_API_KEY"
IMPLEMENTATION_VERSION = "c-series-sealed-matrix-v2"

SEALED_REQUIRED_PREREGISTRATION_FIELDS = frozenset(
    {
        "runtime",
        "cache_root_sha256",
        "snapshot_files",
        "frozen_rules",
        "corpus_seed_sha256",
        "hl_mem_toml_sha256",
        "sealed_payload_sha256",
        "inputs_sha256",
        "packets_sha256",
        "implementation_snapshot",
        "runtime_config_sha256",
        "authorization_override",
        "design_dev_snapshot",
        "readers",
        "repeats",
    }
)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _corpus_seed_sha256(manifest: Mapping[str, Any]) -> str:
    actual = _canonical_hash(manifest["corpora"])
    if manifest.get("corpus_seed_sha256") != actual:
        raise ValueError("sealed corpus seed hash drift")
    return actual


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _safe_name(value: str) -> str:
    if not value.startswith(CURRENT_SUITE.case_prefix) or not value.removeprefix(CURRENT_SUITE.case_prefix).isdigit():
        raise ValueError("sealed case ID is outside the frozen namespace")
    return value


def reader_snapshot(qwen_settings: Any) -> dict[str, dict[str, Any]]:
    """Return public reader configuration without serializing credentials."""
    timeout = float(qwen_settings.llm_timeout)
    return {
        "qwen": {
            "provider": str(qwen_settings.llm_provider),
            "base_url": str(qwen_settings.llm_base_url),
            "model": str(qwen_settings.llm_model),
            "revision": str(qwen_settings.llm_model),
            "endpoint_class": "coding-plan",
            "temperature": 0.1,
            "max_output_tokens": 512,
            "timeout_seconds": timeout,
            "seed_support": "unsupported",
        },
        "glm": {
            "provider": "zhipu",
            "base_url": GLM_BASE_URL,
            "model": GLM_MODEL,
            "revision": GLM_MODEL,
            "endpoint_class": "coding-plan",
            "temperature": 0.1,
            "max_output_tokens": 512,
            "timeout_seconds": timeout,
            "seed_support": "unsupported",
        },
    }


def _model_snapshot(settings: Any) -> dict[str, Any]:
    readers = reader_snapshot(settings)
    return {
        "extractor": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "revision": LLM_EXTRACTOR_VERSION,
            "endpoint_class": "coding-plan",
            "structured_mode": settings.llm_structured_mode,
            "temperature": 0.1,
        },
        "relation_discovery": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "revision": settings.llm_model,
            "endpoint_class": "coding-plan",
            "mode": "auto",
            "temperature": 0.1,
        },
        "embedder": {
            "provider": "dashscope",
            "base_url": settings.embedding_base_url,
            "model": settings.embedding_model,
            "revision": settings.embedding_model,
            "api_mode": settings.embedding_api_mode,
            "dim": settings.embedding_dim,
        },
        "reranker": {
            "provider": settings.reranker_provider,
            "base_url": settings.reranker_base_url,
            "model": settings.reranker_model,
            "revision": settings.reranker_model,
            "mode": settings.reranker_mode,
        },
        "readers": readers,
        "planner": {
            "enabled": False,
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "revision": settings.llm_model,
            "endpoint_class": "coding-plan",
        },
    }


def gold_free_case(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Select the only sealed fields allowed to reach cache/recall/reader code."""
    result = {
        "case_id": str(raw["case_id"]),
        "category": str(raw["category"]),
        "namespace": str(raw["namespace"]),
        "events": [dict(item) for item in raw.get("events") or []],
        "question_at": str(raw["question_at"]),
        "known_as_of": None,
        "question": str(raw["question"]),
        "relation_coverage": str(
            raw.get("relation_coverage") or ("none" if str(raw.get("category")) == "no_answer_trap" else "required")
        ),
    }
    assert_gold_free(result)
    return result


def _relation_count(database: Path) -> int:
    if not database.is_file():
        raise RuntimeError(f"sealed relation cache is missing: {database}")
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT COUNT(*) FROM memory_relations").fetchone()
    finally:
        connection.close()
    return int(row[0]) if row else 0


def validate_relation_coverage(cases: Sequence[Mapping[str, Any]], databases: Mapping[str, Path]) -> dict[str, Any]:
    """Require each cache to match its frozen relation-coverage declaration."""
    by_case: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for case in cases:
        case_id = str(case["case_id"])
        declared = str(case.get("relation_coverage") or "")
        if declared not in {"required", "none"}:
            raise RuntimeError(f"relation coverage gate has invalid declaration: {case_id}={declared!r}")
        database = databases.get(case_id)
        if database is None:
            raise RuntimeError(f"relation coverage gate has no cache path: {case_id}")
        count = _relation_count(database)
        by_case[case_id] = {"declared": declared, "relations": count}
        if (declared == "required" and count == 0) or (declared == "none" and count != 0):
            failures.append(f"{case_id}:{declared}={count}")
    if failures:
        raise RuntimeError(f"relation coverage gate failed: {', '.join(failures)}")
    ordered = dict(sorted(by_case.items()))
    return {
        "required_cases": sum(item["declared"] == "required" for item in ordered.values()),
        "required_with_edges": sum(
            item["declared"] == "required" and int(item["relations"]) > 0 for item in ordered.values()
        ),
        "none_cases": sum(item["declared"] == "none" for item in ordered.values()),
        "none_with_edges": sum(item["declared"] == "none" and int(item["relations"]) > 0 for item in ordered.values()),
        "total_relations": sum(int(item["relations"]) for item in ordered.values()),
        "by_case": ordered,
    }


def assert_c0_c4_packet_smoke(
    packet_snapshot: Mapping[str, Any], required_case_ids: Sequence[str], preregistration_id: str
) -> dict[str, Any]:
    """Prove C4 changes three deterministically sampled required-case packets."""
    unique_ids = sorted(set(str(case_id) for case_id in required_case_ids))
    if len(unique_ids) < 3:
        raise RuntimeError("packet smoke requires at least 3 relation-required cases")
    sampled = sorted(
        unique_ids,
        key=lambda case_id: hashlib.sha256(f"{preregistration_id}{case_id}".encode()).hexdigest(),
    )[:3]
    packets = {
        (str(item["case_id"]), int(item["repeat_index"]), str(item["arm_id"])): item
        for item in packet_snapshot.get("packets") or []
    }
    equal_pairs: list[str] = []
    digests: dict[str, dict[str, str]] = {}
    for case_id in sampled:
        try:
            c0 = packets[(case_id, 0, "C0")]["packet"]
            c4 = packets[(case_id, 0, "C4")]["packet"]
        except KeyError as error:
            raise RuntimeError(f"packet smoke is missing a frozen pair: {case_id}") from error
        if c0 == c4:
            equal_pairs.append(case_id)
        digests[case_id] = {"C0": _canonical_hash(c0), "C4": _canonical_hash(c4)}
    if equal_pairs:
        raise RuntimeError(f"C0/C4 packet smoke failed for equal packets: {', '.join(equal_pairs)}")
    return {"passed": True, "case_ids": sampled, "equal_pairs": [], "packet_sha256": digests}


def _safe_holdout_cases() -> tuple[list[dict[str, Any]], Path, str]:
    manifest = load_holdout_manifest(HOLDOUT_MANIFEST)
    payload_path = resolve_holdout_path(manifest)
    dataset = load_sealed_holdout(HOLDOUT_MANIFEST, allow_sealed=True)
    cases = [gold_free_case(dataclasses.asdict(case)) for case in dataset.cases]
    return cases, payload_path.resolve(), manifest.sha256


def _case_fingerprint(case: Mapping[str, Any]) -> str:
    return _canonical_hash(case)


def freeze_case_catalog(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    case_ids = [str(case["case_id"]) for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("case catalog contains duplicate IDs")
    return {
        "case_count": len(case_ids),
        "case_ids": sorted(case_ids),
        "category_distribution": dict(sorted(Counter(str(case["category"]) for case in cases).items())),
        "dataset_distribution": dict(sorted(Counter(str(case["dataset"]) for case in cases).items())),
    }


def _cache_config(settings: Any) -> dict[str, Any]:
    return {
        "extractor": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "version": LLM_EXTRACTOR_VERSION,
            "structured_mode": settings.llm_structured_mode,
            "verification_mode": settings.verification_mode,
        },
        "embedding": {
            "base_url": settings.embedding_base_url,
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
            "mode": settings.embedder_mode,
        },
        "relation_discovery": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "mode": "auto",
            "pool_limit": settings.relation_discovery_pool_limit,
            "max_proposals": settings.relation_discovery_max_proposals,
            "auto_apply_confidence": settings.relation_auto_apply_confidence,
            "conflict_confidence": settings.relation_conflict_confidence,
        },
    }


def _cache_paths(case_id: str) -> tuple[Path, Path]:
    root = CACHE_ROOT.resolve()
    database = (CACHE_ROOT / f"{_safe_name(case_id)}.db").resolve()
    manifest = database.with_suffix(".manifest.json")
    if not database.is_relative_to(root) or not manifest.is_relative_to(root):
        raise RuntimeError("sealed cache path escaped its root")
    return database, manifest


def _cache_valid(database: Path, manifest_path: Path, fingerprint: str) -> bool:
    if not database.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = _json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(manifest.get("fingerprint") == fingerprint and manifest.get("db_sha256") == sha256_file(database))


def _remove_cache_artifacts(database: Path, manifest: Path) -> None:
    root = CACHE_ROOT.resolve()
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm"), manifest):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved.parent != database.parent:
            raise RuntimeError("refusing to remove cache artifact outside sealed cache root")
        resolved.unlink(missing_ok=True)


def _ingest_case(database_path: Path, case: Mapping[str, Any], settings: Any, embedder: Any) -> dict[str, Any]:
    relation_settings = dataclasses.replace(
        settings,
        relation_discovery_mode="auto",
        query_expansion_mode="off",
    )
    relation_settings.validate()
    database = Database(database_path, settings=relation_settings)
    extracted = 0
    stored = 0
    extraction_tokens = 0
    relation_counts: Counter[str] = Counter()
    try:
        with database.connect() as connection:
            service = IngestService(connection)
            extractor = make_extractor(relation_settings, require_real=True, connection=connection)
            for raw_event in case["events"]:
                event = {
                    "id": str(raw_event["event_id"]),
                    "idempotency_key": f"sealed:{case['case_id']}:{raw_event['event_id']}",
                    "tenant_id": str(case["namespace"]),
                    "event_type": "message",
                    "actor_type": "user",
                    "content": {
                        "text": str(raw_event["text"]),
                        "sealed_locator": {
                            "case_id": str(case["case_id"]),
                            "event_id": str(raw_event["event_id"]),
                        },
                    },
                    "occurred_at": str(raw_event["occurred_at"]),
                    "recorded_at": str(raw_event["occurred_at"]),
                }
                service.ingest_event(event)
                event["extractor"] = "llm"
                event["extractor_version"] = getattr(extractor, "extractor_version", LLM_EXTRACTOR_VERSION)
                claims = extractor.extract(
                    event["content"],
                    {
                        "actor_type": "user",
                        "event_type": "message",
                        "occurred_at": event["occurred_at"],
                    },
                )
                extracted += len(claims)
                extraction_tokens += int(getattr(extractor, "last_usage_tokens", 0))
                now = datetime.now(timezone.utc).isoformat()
                for claim in claims:
                    result = IngestService.store_extracted(
                        connection,
                        claim,
                        event,
                        now,
                        embedder,
                        policy=relation_settings.retention_policy(),
                        relation_discovery_mode="off",
                        index_text_mode=relation_settings.index_text_mode,
                    )
                    if result.status != "skipped":
                        stored += 1
            discoverer = make_relation_discoverer(relation_settings, connection)
            if discoverer is None:
                raise RuntimeError("sealed cache requires relation discovery=auto")
            claim_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM claims WHERE status IN ('active','disputed') ORDER BY recorded_from,id"
                ).fetchall()
            ]
            for claim_id in claim_ids:
                counts = discover_relations(
                    connection,
                    discoverer,
                    claim_id,
                    mode="auto",
                    pool_limit=relation_settings.relation_discovery_pool_limit,
                    max_proposals=relation_settings.relation_discovery_max_proposals,
                    auto_apply_confidence=relation_settings.relation_auto_apply_confidence,
                    conflict_confidence=relation_settings.relation_conflict_confidence,
                )
                relation_counts.update(counts)
            rows = connection.execute(
                "SELECT relation,COUNT(*) FROM memory_relations GROUP BY relation ORDER BY relation"
            ).fetchall()
            applied_by_type = {str(row[0]): int(row[1]) for row in rows}
    finally:
        database.close()
    return {
        "events": len(case["events"]),
        "claims_extracted": extracted,
        "claims_stored": stored,
        "extraction_tokens": extraction_tokens,
        "relation_discovery": dict(sorted(relation_counts.items())),
        "relations_applied_by_type": applied_by_type,
    }


def command_prepare_cache() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    if "coding" not in settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("sealed extraction/relation discovery requires configured coding-plan qwen key")
    cases, payload_path, payload_sha = _safe_holdout_cases()
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    initialize_process(settings)
    embedder = make_embedder(settings)
    config = _cache_config(settings)
    databases: dict[str, Path] = {}
    reused = 0
    built = 0
    for index, case in enumerate(cases, start=1):
        database, manifest_path = _cache_paths(str(case["case_id"]))
        databases[str(case["case_id"])] = database
        fingerprint = _canonical_hash(
            {
                "case": case,
                "payload_sha256": payload_sha,
                "config": config,
                "ingest_source_sha256": sha256_file(Path(__file__)),
                "base_runtime_sha256": sha256_file(ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py"),
            }
        )
        if _cache_valid(database, manifest_path, fingerprint):
            reused += 1
            print(json.dumps({"case": case["case_id"], "cache": "reused"}), flush=True)
            continue
        _remove_cache_artifacts(database, manifest_path)
        stats = _ingest_case(database, case, settings, embedder)
        manifest = {
            "schema_version": 1,
            "case_id": case["case_id"],
            "case_fingerprint": _case_fingerprint(case),
            "payload_identity": str(payload_path),
            "payload_sha256": payload_sha,
            "fingerprint": fingerprint,
            "db_sha256": sha256_file(database),
            "contains_gold": False,
            "relation_coverage": case["relation_coverage"],
            "config": config,
            "stats": stats,
        }
        write_json_atomic(manifest_path, manifest)
        built += 1
        print(json.dumps({"case": case["case_id"], "cache": "built", "index": index, **stats}), flush=True)
    result: dict[str, Any] = {"cases": len(cases), "built": built, "reused": reused}
    if CURRENT_SUITE.version == "v2":
        result["relation_coverage"] = validate_relation_coverage(cases, databases)
    print(json.dumps(result))
    return 0


def _runtime_case(case: Mapping[str, Any], database: Path, payload_sha: str) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "dataset": "relation_sealed",
        "category": case["category"],
        "question": case["question"],
        "relation_coverage": case["relation_coverage"],
        "question_at": case["question_at"],
        "known_as_of": case.get("known_as_of"),
        "namespace": case["namespace"],
        "db_path": str(database.resolve()),
        "allowed_modalities": ["text"],
        "source_cache_identity": str(database.resolve()),
        "source_cache_sha256": sha256_file(database),
        "source_corpora": [{"id": "sealed_holdout", "sha256": payload_sha}],
    }


def _packet_fingerprint(
    inputs: Mapping[str, Any],
    settings: Any,
    caches: Mapping[str, str],
    preregistration_id: str,
    corpus_seed_sha256: str,
) -> str:
    return _canonical_hash(
        {
            "inputs": inputs,
            "settings": base._runtime_fingerprint(settings),
            "caches": caches,
            "preregistration_id": preregistration_id,
            "corpus_seed_sha256": corpus_seed_sha256,
            "arms": ARMS,
            "repeats": REPEATS,
            "runtime_sha256": sha256_file(ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py"),
            "protocol_sha256": sha256_file(ROOT / "src" / "hl_mem" / "evaluation" / "c_series.py"),
        }
    )


def _packet_key(case_id: str, repeat: int, arm: str) -> str:
    return f"{case_id}|{repeat}|{arm}"


def _prepare_packets(
    inputs: Mapping[str, Any],
    settings: Any,
    caches: Mapping[str, str],
    preregistration_id: str,
    corpus_sha: str,
) -> dict[str, Any]:
    fingerprint = _packet_fingerprint(inputs, settings, caches, preregistration_id, corpus_sha)
    if PACKETS.is_file():
        raw_existing = _json(PACKETS)
        existing = dict(raw_existing) if isinstance(raw_existing, Mapping) else {}
        if existing.get("fingerprint") == fingerprint:
            expected = len(inputs["cases"]) * REPEATS * len(ARMS)
            if len(existing.get("packets") or []) == expected:
                return existing
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    packets: list[dict[str, Any]] = []
    for case in inputs["cases"]:
        for repeat in range(REPEATS):
            seed = case_seed(preregistration_id, corpus_sha, str(case["case_id"]), repeat)
            arms = sorted(ARMS, key=lambda arm: hashlib.sha256(f"{seed}{arm}".encode()).hexdigest())
            for arm in arms:
                execution = recall_visible_case(
                    case,
                    settings,
                    embedder,
                    reranker,
                    db_path=Path(case["db_path"]),
                    arm_id=arm,
                )
                packets.append(
                    {
                        "packet_key": _packet_key(str(case["case_id"]), repeat, arm),
                        "case_id": case["case_id"],
                        "repeat_index": repeat,
                        "arm_id": arm,
                        "packet": list(execution.packet),
                        "top5_seed_packet": list(execution.seed_packet),
                        "answerability": execution.answerability,
                        "recall_latency_seconds": execution.recall_latency_seconds,
                    }
                )
                print(
                    json.dumps({"packet": len(packets), "case": case["case_id"], "repeat": repeat, "arm": arm}),
                    flush=True,
                )
    result = {"schema_version": 1, "fingerprint": fingerprint, "packets": packets}
    assert_gold_free(result)
    write_json_atomic(PACKETS, result)
    return result


def _implementation_snapshot() -> dict[str, str]:
    files = {
        "sealed_runner": Path(__file__),
        "sealed_scorer": ROOT / "evaluation" / "tools" / "score_c_series_sealed_experiment.py",
        "base_runner": ROOT / "evaluation" / "tools" / "run_c_series_relation_experiment.py",
        "base_scorer": ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
        "runtime": ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        "protocol": ROOT / "src" / "hl_mem" / "evaluation" / "c_series.py",
        "sealed_holdout_loader": ROOT / "tests" / "eval" / "relation_chain_holdout.py",
    }
    return {
        "version": IMPLEMENTATION_VERSION,
        **{f"{name}_sha256": sha256_file(path) for name, path in files.items()},
    }


def _assert_suite_binding(manifest: Mapping[str, Any], expected_suite: str) -> str:
    declared = manifest.get("suite_version")
    if declared is None:
        if expected_suite == "v1":
            return "v1"
        raise ValueError("sealed v2 preregistration requires suite_version")
    if declared not in {"v1", "v2"}:
        raise ValueError(f"sealed suite_version is invalid: {declared!r}")
    if declared != expected_suite:
        raise ValueError(f"sealed suite mismatch: expected {expected_suite}, got {declared}")
    return str(declared)


def _validate_preregistration(manifest: Mapping[str, Any], *, expected_suite: str | None = None) -> None:
    suite_version = _assert_suite_binding(manifest, expected_suite or CURRENT_SUITE.version)
    base.validate_preregistration(manifest)
    missing = SEALED_REQUIRED_PREREGISTRATION_FIELDS - manifest.keys()
    if missing:
        raise ValueError(f"sealed preregistration fields missing: {sorted(missing)}")
    if manifest["protocol_version"] != PROTOCOL_VERSION or manifest["scorer_version"] != "answer-entity-packet-v1":
        raise ValueError("sealed protocol/scorer version mismatch")
    arms = manifest.get("arms")
    if not isinstance(arms, Mapping) or tuple(arms) != ARMS:
        raise ValueError("sealed arm specifications drifted")
    if tuple(manifest["readers"]) != READERS or manifest["repeats"] != REPEATS:
        raise ValueError("sealed matrix dimensions drifted")
    runtime = manifest.get("runtime") or {}
    if not {"python", "sqlite", "os", "timezone", "unicode_normalization"} <= runtime.keys():
        raise ValueError("sealed runtime snapshot is incomplete")
    required_corpora = {
        "chinese_e2e_manifest",
        "visible_relation_dev",
        "intent_routing_dev",
        "sealed_holdout",
        "sealed_holdout_manifest",
        "gold_free_inputs",
    }
    if not required_corpora <= manifest["corpora"].keys():
        raise ValueError("sealed design/dev/corpus snapshot is incomplete")
    frozen = manifest.get("frozen_rules") or {}
    if (
        not {
            "intent_version",
            "sufficiency_version",
            "sufficiency",
            "relation_allowlist",
            "relation_weight",
            "top_seed_limit",
            "final_claim_limit",
            "packet_token_budget",
            "path_token_budget",
            "repeats",
            "top5_seed_definition",
            "tie_breaker",
            "relation_expansion_arm",
            "relation_hop_decay",
            "question_time_contract",
        }
        <= frozen.keys()
    ):
        raise ValueError("sealed frozen relation/budget rules are incomplete")
    if int(frozen["repeats"]) != REPEATS:
        raise ValueError("sealed frozen repeat count drifted")
    models = manifest.get("models") or {}
    if not {"extractor", "relation_discovery", "embedder", "reranker", "readers", "planner"} <= models.keys():
        raise ValueError("sealed model snapshot is incomplete")
    if tuple(models["readers"]) != READERS:
        raise ValueError("sealed reader model snapshot drifted")
    if not (manifest.get("authorization_override") or {}).get("authorized"):
        raise ValueError("sealed authorization override is missing")
    if suite_version == "v2":
        coverage = manifest.get("relation_coverage") or {}
        if (
            int(coverage.get("required_cases") or 0) < 3
            or coverage.get("required_cases") != coverage.get("required_with_edges")
            or int(coverage.get("none_with_edges") or 0) != 0
        ):
            raise ValueError("sealed v2 relation coverage gate is not satisfied")
        smoke = manifest.get("packet_smoke") or {}
        if smoke.get("passed") is not True or len(smoke.get("case_ids") or []) != 3 or smoke.get("equal_pairs"):
            raise ValueError("sealed v2 packet smoke gate is not satisfied")
    design_dev = manifest.get("design_dev_snapshot") or {}
    if design_dev.get("case_count") != 52 or len(design_dev.get("case_ids") or []) != 52:
        raise ValueError("sealed design/dev case catalog is incomplete")
    if sum((design_dev.get("category_distribution") or {}).values()) != 52:
        raise ValueError("sealed design/dev category distribution is incomplete")
    uv_lock = str((ROOT / "uv.lock").resolve())
    if (manifest.get("snapshot_files") or {}).get(uv_lock) != sha256_file(ROOT / "uv.lock"):
        raise ValueError("sealed uv.lock snapshot is missing")
    _corpus_seed_sha256(manifest)


def verify_public_snapshot_files(manifest: Mapping[str, Any]) -> None:
    """Verify tracked public metadata without opening the sealed payload."""
    expected = str((manifest.get("corpora") or {}).get("sealed_holdout_manifest") or "")
    if not HOLDOUT_MANIFEST.is_file() or sha256_file(HOLDOUT_MANIFEST) != expected:
        raise RuntimeError("sealed holdout manifest drift")


def command_preregister() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("sealed preregistration requires clean committed source")
    commit = _git("rev-parse", "HEAD")
    safe_cases, payload_path, payload_sha = _safe_holdout_cases()
    visible_design = _json(base.DESIGN)["cases"]
    design_dev_snapshot = freeze_case_catalog(
        [
            *base._e2e_inputs(),
            *(
                {
                    "case_id": str(case["case_id"]),
                    "category": str(case["category"]),
                    "dataset": "relation_design_dev",
                }
                for case in visible_design
            ),
        ]
    )
    cache_files: dict[str, str] = {}
    cache_paths: list[Path] = []
    databases: dict[str, Path] = {}
    runtime_cases: list[dict[str, Any]] = []
    cache_manifests: list[Path] = []
    for case in safe_cases:
        database, manifest_path = _cache_paths(str(case["case_id"]))
        if not database.is_file() or not manifest_path.is_file():
            raise RuntimeError("run prepare-cache before sealed preregistration")
        manifest = _json(manifest_path)
        if manifest.get("contains_gold") is not False or manifest.get("db_sha256") != sha256_file(database):
            raise RuntimeError(f"sealed cache manifest invalid: {case['case_id']}")
        cache_files[str(database.resolve())] = sha256_file(database)
        databases[str(case["case_id"])] = database
        cache_files[str(manifest_path.resolve())] = sha256_file(manifest_path)
        cache_paths.extend((database.resolve(), manifest_path.resolve()))
        cache_manifests.append(manifest_path)
        runtime_cases.append(_runtime_case(case, database, payload_sha))
    relation_coverage = validate_relation_coverage(safe_cases, databases) if CURRENT_SUITE.version == "v2" else None
    inputs = {"schema_version": 1, "protocol_version": PROTOCOL_VERSION, "cases": runtime_cases}
    assert_gold_free(inputs)
    write_json_atomic(INPUTS, inputs)
    preregistration_id = f"c-series-sealed-c4-reader-matrix-{CURRENT_SUITE.version}-{commit[:12]}"
    corpus_paths = {
        **base._source_corpora(),
        "sealed_holdout": payload_path,
        "sealed_holdout_manifest": HOLDOUT_MANIFEST,
        "gold_free_inputs": INPUTS,
    }
    manifest = base.build_preregistration(
        preregistration_id=preregistration_id,
        git_commit=commit,
        clean_source=True,
        corpus_paths=corpus_paths,
        cache_paths=cache_paths,
        model_snapshot=_model_snapshot(settings),
        prompt_hashes=base._prompt_hashes(),
        case_ids=[str(case["case_id"]) for case in runtime_cases],
    )
    manifest["arms"] = {arm: dataclasses.asdict(arm_spec(arm)) for arm in ARMS}
    manifest["readers"] = list(READERS)
    manifest["repeats"] = REPEATS
    manifest["corpus_seed_sha256"] = _canonical_hash(manifest["corpora"])
    manifest["frozen_rules"].update(
        {
            "top5_seed_definition": "base fusion plus multifactor rank, pre_rank<=5",
            "tie_breaker": "claim_id ascending",
            "relation_expansion_arm": dataclasses.asdict(arm_spec("C4")),
            "relation_hop_decay": "relation_weight/2 per hop",
            "question_time_contract": "question_at/as_of/known_as_of with Asia/Shanghai manifest timezone",
        }
    )
    packet_snapshot = _prepare_packets(
        inputs,
        settings,
        cache_files,
        preregistration_id,
        _corpus_seed_sha256(manifest),
    )
    packet_smoke = None
    if CURRENT_SUITE.version == "v2":
        required_case_ids = [
            str(case["case_id"]) for case in safe_cases if str(case["relation_coverage"]) == "required"
        ]
        packet_smoke = assert_c0_c4_packet_smoke(packet_snapshot, required_case_ids, preregistration_id)
    manifest.update(
        {
            "schema_version": 1,
            "suite_version": CURRENT_SUITE.version,
            "sealed_payload_identity": str(payload_path),
            "sealed_payload_sha256": payload_sha,
            "category_distribution": dict(sorted(Counter(case["category"] for case in runtime_cases).items())),
            "design_dev_snapshot": design_dev_snapshot,
            "cache_manifests": {
                str(path.resolve()): {
                    key: value
                    for key, value in _json(path).items()
                    if key
                    in {"case_id", "case_fingerprint", "payload_sha256", "fingerprint", "db_sha256", "config", "stats"}
                }
                for path in cache_manifests
            },
            "inputs_sha256": sha256_file(INPUTS),
            "packets_sha256": sha256_file(PACKETS),
            "packet_fingerprint": packet_snapshot["fingerprint"],
            "packet_count": len(packet_snapshot["packets"]),
            **(
                {"relation_coverage": relation_coverage, "packet_smoke": packet_smoke}
                if CURRENT_SUITE.version == "v2"
                else {}
            ),
            "runtime_config_sha256": base._runtime_fingerprint(settings),
            "implementation_snapshot": _implementation_snapshot(),
            "hl_mem_toml_sha256": sha256_file(ROOT / "hl_mem.toml"),
            "snapshot_files": {
                **base._snapshot_files(corpus_paths, cache_paths),
                str(PACKETS.resolve()): sha256_file(PACKETS),
            },
            "seed_rule": "first64bits(SHA256(preregistration_id||corpus_seed_sha256||case_id||repeat_index))",
            "matrix_order_rule": "SHA256(case_seed||arm_id||reader_id)",
            "packet_budget": {"claims": 10, "tokens": 2000},
            "authorization_override": {
                "authorized": True,
                "selected_arm": "C4",
                "reason": "user-approved sealed validation after external frozen-packet glm-5.3 reader comparison",
                "design_dev_gate_was_not_passed": True,
                "reader_matrix_extension": True,
                **({"relation_coverage_gate": True} if CURRENT_SUITE.version == "v2" else {}),
            },
        }
    )
    _validate_preregistration(manifest)
    write_json_atomic(PREREG, manifest)
    print(
        json.dumps(
            {
                "cases": len(runtime_cases),
                "packets": len(packet_snapshot["packets"]),
                "tasks": len(build_tasks(manifest, inputs)),
                "preregistration": str(PREREG),
            }
        )
    )
    return 0


def build_tasks(
    manifest: Mapping[str, Any], inputs: Mapping[str, Any]
) -> list[tuple[Mapping[str, Any], int, str, str]]:
    corpus_sha = _corpus_seed_sha256(manifest)
    tasks: list[tuple[Mapping[str, Any], int, str, str]] = []
    for case in inputs["cases"]:
        for repeat in range(int(manifest.get("repeats", REPEATS))):
            seed = case_seed(str(manifest["preregistration_id"]), corpus_sha, str(case["case_id"]), repeat)
            cells = sorted(
                ((arm, reader) for arm in ARMS for reader in READERS),
                key=lambda cell: hashlib.sha256(f"{seed}{cell[0]}{cell[1]}".encode()).hexdigest(),
            )
            tasks.extend((case, repeat, arm, reader) for arm, reader in cells)
    return tasks


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                break
            raise
    return rows


def repair_jsonl_tail(path: Path) -> bool:
    """Make a crash-truncated JSONL tail append-safe without dropping valid rows."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    data = path.read_bytes()
    offset = 0
    lines = data.splitlines(keepends=True)
    for index, raw_line in enumerate(lines):
        content = raw_line.rstrip(b"\r\n")
        if not content.strip():
            offset += len(raw_line)
            continue
        try:
            json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            is_partial_tail = index == len(lines) - 1 and not raw_line.endswith((b"\n", b"\r"))
            if not is_partial_tail:
                raise RuntimeError(f"malformed sealed JSONL before tail: line {index + 1}")
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        offset += len(raw_line)
    if not data.endswith((b"\n", b"\r")):
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    return False


def completed_matrix_keys(rows: Sequence[Mapping[str, Any]]) -> set[tuple[str, int, str, str]]:
    return {
        (str(row["case_id"]), int(row["repeat_index"]), str(row["arm_id"]), str(row["reader_id"]))
        for row in rows
        if row.get("status") == "complete"
    }


def _raw_binding(manifest: Mapping[str, Any], preregistration_sha256: str, reader_id: str) -> dict[str, str]:
    reader = (manifest.get("models") or {}).get("readers", {}).get(reader_id)
    if not isinstance(reader, Mapping):
        raise RuntimeError(f"sealed reader snapshot missing: {reader_id}")
    return {
        "preregistration_id": str(manifest["preregistration_id"]),
        "preregistration_sha256": preregistration_sha256,
        "reader_snapshot_sha256": _canonical_hash(reader),
    }


def verify_resume_bindings(
    rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any], preregistration_sha256: str
) -> None:
    for row in rows:
        expected = _raw_binding(manifest, preregistration_sha256, str(row.get("reader_id") or ""))
        if any(row.get(key) != value for key, value in expected.items()):
            raise RuntimeError("sealed raw belongs to a different preregistration or reader snapshot")


def _verify_live_snapshot(manifest: Mapping[str, Any], settings: Any) -> None:
    _validate_preregistration(manifest, expected_suite=CURRENT_SUITE.version)
    verify_public_snapshot_files(manifest)
    if _git("rev-parse", "HEAD") != manifest["git_commit"]:
        raise RuntimeError("git commit differs from sealed preregistration")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("sealed live requires clean source")
    if manifest["implementation_snapshot"] != _implementation_snapshot():
        raise RuntimeError("sealed implementation snapshot drift")
    if manifest["prompt_hashes"] != base._prompt_hashes():
        raise RuntimeError("sealed QA prompt drift")
    if manifest["runtime_config_sha256"] != base._runtime_fingerprint(settings):
        raise RuntimeError("sealed runtime configuration drift")
    if manifest["hl_mem_toml_sha256"] != sha256_file(ROOT / "hl_mem.toml"):
        raise RuntimeError("hl_mem.toml drift")
    if manifest["inputs_sha256"] != sha256_file(INPUTS) or manifest["packets_sha256"] != sha256_file(PACKETS):
        raise RuntimeError("sealed gold-free input/packet snapshot drift")
    for path, expected in manifest["cache_files"].items():
        candidate = Path(path)
        if not candidate.is_file() or sha256_file(candidate) != expected:
            raise RuntimeError(f"sealed cache drift: {candidate}")
    assert_gold_free(_json(INPUTS))
    packet_snapshot = _json(PACKETS)
    assert_gold_free(packet_snapshot)
    if CURRENT_SUITE.version == "v2":
        inputs = _json(INPUTS)
        required_case_ids = [
            str(case["case_id"])
            for case in inputs.get("cases") or []
            if str(case.get("relation_coverage")) == "required"
        ]
        smoke = assert_c0_c4_packet_smoke(
            packet_snapshot,
            required_case_ids,
            str(manifest["preregistration_id"]),
        )
        if smoke != manifest.get("packet_smoke"):
            raise RuntimeError("sealed v2 packet smoke snapshot drift")


def _reader_settings(settings: Any, reader_id: str) -> Any:
    if reader_id == "qwen":
        return settings
    if reader_id != "glm":
        raise ValueError(f"unknown sealed reader: {reader_id}")
    api_key = os.environ.get(GLM_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{GLM_KEY_ENV} is required for glm sealed reader")
    return dataclasses.replace(
        settings,
        llm_provider="zhipu",
        llm_base_url=GLM_BASE_URL,
        llm_model=GLM_MODEL,
        llm_api_key=api_key,
    )


def _e2e_latency(recall_latency_seconds: float, reader_wall_seconds: float) -> float:
    return float(recall_latency_seconds) + float(reader_wall_seconds)


async def _run_one_reader(
    client: Any,
    semaphore: asyncio.Semaphore,
    case: Mapping[str, Any],
    repeat: int,
    arm: str,
    reader_id: str,
    settings: Any,
    packet_snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
    preregistration_sha256: str,
) -> dict[str, Any]:
    async with semaphore:
        reader_settings = _reader_settings(settings, reader_id)
        packet = list(packet_snapshot["packet"])
        context = "\n".join(f"[{index}] {item['text']}" for index, item in enumerate(packet, start=1))
        started = time.perf_counter()
        predicted, usage = await base._chat(
            client,
            reader_settings,
            system=base.QA_SYSTEM,
            user=base.QA_USER_TEMPLATE.format(packet=context or "(empty)", question=case["question"]),
            max_tokens=512,
            timeout=float(reader_settings.llm_timeout),
        )
        reader_latency = time.perf_counter() - started
        return {
            "status": "complete",
            **_raw_binding(manifest, preregistration_sha256, reader_id),
            "case_id": case["case_id"],
            "dataset": "relation_sealed",
            "category": case["category"],
            "repeat_index": repeat,
            "arm_id": arm,
            "reader_id": reader_id,
            "predicted_answer": predicted,
            "packet": packet,
            "top5_seed_packet": list(packet_snapshot["top5_seed_packet"]),
            "answerability": packet_snapshot["answerability"],
            "recall_latency_seconds": float(packet_snapshot["recall_latency_seconds"]),
            "reader_latency_seconds": reader_latency,
            "e2e_latency_seconds": _e2e_latency(packet_snapshot["recall_latency_seconds"], reader_latency),
            "usage": usage,
            "reader_seed": None,
            "seed_support": "unsupported",
        }


async def _run_with_retry(*args: Any) -> dict[str, Any]:
    task_started = time.perf_counter()
    errors: list[str] = []
    for attempt in range(1, 4):
        try:
            result = await _run_one_reader(*args)
            result["attempts"] = attempt
            result["retry_errors"] = errors
            result["e2e_latency_seconds"] = _e2e_latency(
                result["recall_latency_seconds"], time.perf_counter() - task_started
            )
            return result
        except Exception as error:
            if not is_retryable_error(error) or attempt == 3:
                raise
            errors.append(type(error).__name__)
    raise AssertionError("unreachable")


async def command_live(concurrency: int) -> int:
    if not 1 <= concurrency <= 4:
        raise ValueError("sealed concurrency must be between 1 and 4")
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    manifest = _json(PREREG)
    preregistration_sha256 = sha256_file(PREREG)
    _verify_live_snapshot(manifest, settings)
    if "coding" not in settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("qwen coding-plan endpoint/key required")
    _reader_settings(settings, "glm")
    inputs = _json(INPUTS)
    packets = {str(item["packet_key"]): item for item in _json(PACKETS)["packets"]}
    repair_jsonl_tail(RAW)
    existing_rows = _read_jsonl(RAW)
    verify_resume_bindings(existing_rows, manifest, preregistration_sha256)
    completed = completed_matrix_keys(existing_rows)
    pending = [
        task
        for task in build_tasks(manifest, inputs)
        if (str(task[0]["case_id"]), task[1], task[2], task[3]) not in completed
    ]
    import httpx

    semaphore = asyncio.Semaphore(concurrency)
    with RAW.open("a", encoding="utf-8") as handle:
        async with httpx.AsyncClient() as client:
            for offset in range(0, len(pending), concurrency):
                batch = pending[offset : offset + concurrency]
                results = await asyncio.gather(
                    *(
                        _run_with_retry(
                            client,
                            semaphore,
                            case,
                            repeat,
                            arm,
                            reader,
                            settings,
                            packets[_packet_key(str(case["case_id"]), repeat, arm)],
                            manifest,
                            preregistration_sha256,
                        )
                        for case, repeat, arm, reader in batch
                    ),
                    return_exceptions=True,
                )
                for task, result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException):
                        case, repeat, arm, reader = task
                        record = {
                            "status": "retryable_error" if is_retryable_error(result) else "fatal_error",
                            **_raw_binding(manifest, preregistration_sha256, reader),
                            "case_id": case["case_id"],
                            "repeat_index": repeat,
                            "arm_id": arm,
                            "reader_id": reader,
                            "error_class": type(result).__name__,
                            "error": str(result)[:500],
                        }
                    else:
                        record = result
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if any(isinstance(item, BaseException) and not is_retryable_error(item) for item in results):
                    raise RuntimeError("fatal sealed live error recorded")
    complete = len(completed_matrix_keys(_read_jsonl(RAW)))
    remaining = len(build_tasks(manifest, inputs)) - complete
    print(json.dumps({"completed": complete, "remaining": remaining}))
    return 0 if remaining == 0 else 75


def command_dry_run() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    manifest = _json(PREREG)
    _verify_live_snapshot(manifest, settings)
    inputs = _json(INPUTS)
    packet_snapshot = _json(PACKETS)
    packets = packet_snapshot["packets"]
    if len(packets) != len(inputs["cases"]) * REPEATS * len(ARMS):
        raise RuntimeError("sealed packet snapshot is incomplete")
    for item in packets:
        packet = item["packet"]
        if len(packet) > 10 or sum(int(row["token_count"]) for row in packet) > 2000:
            raise RuntimeError(f"sealed packet budget failed: {item['packet_key']}")
    print(
        json.dumps(
            {
                "network_calls": 0,
                "cases": len(inputs["cases"]),
                "packets": len(packets),
                "tasks": len(build_tasks(manifest, inputs)),
            }
        )
    )
    return 0


def command_score() -> int:
    scorer = ROOT / "evaluation" / "tools" / "score_c_series_sealed_experiment.py"
    return subprocess.run(
        [
            sys.executable,
            str(scorer),
            "--suite",
            CURRENT_SUITE.version,
            "--raw",
            str(RAW),
            "--inputs",
            str(INPUTS),
            "--packets",
            str(PACKETS),
            "--holdout-manifest",
            str(HOLDOUT_MANIFEST),
            "--prereg",
            str(PREREG),
            "--output",
            str(REPORT),
            "--markdown",
            str(REPORT_MD),
        ],
        cwd=ROOT,
        check=False,
    ).returncode


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("v1", "v2"), default="v1")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare-cache")
    sub.add_parser("preregister")
    sub.add_parser("dry-run")
    live = sub.add_parser("live")
    live.add_argument("--concurrency", type=int, default=4)
    sub.add_parser("score")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_suite(args.suite)
    if args.command == "prepare-cache":
        return command_prepare_cache()
    if args.command == "preregister":
        return command_preregister()
    if args.command == "dry-run":
        return command_dry_run()
    if args.command == "live":
        return asyncio.run(command_live(args.concurrency))
    return command_score()


if __name__ == "__main__":
    raise SystemExit(main())
