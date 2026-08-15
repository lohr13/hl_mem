#!/usr/bin/env python
"""Run the frozen v0.28 old/new compact extraction A/B on visible 52 cases."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import json
import re
import sqlite3
import subprocess
import sys
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools import score_c_series_relation_experiment as frozen_scorer  # noqa: E402
from hl_mem.application.ingest import IngestService  # noqa: E402
from hl_mem.components import (  # noqa: E402
    initialize_process,
    make_embedder,
    make_relation_discoverer,
    make_reranker,
)
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.domain.claims.attributes import SLOT_REGISTRY  # noqa: E402
from hl_mem.evaluation.c_series import sha256_file, write_json_atomic  # noqa: E402
from hl_mem.evaluation.c_series_runtime import (  # noqa: E402
    assert_gold_free,
    recall_visible_case,
    render_packet_context,
)
from hl_mem.evaluation.extraction_ab import (  # noqa: E402
    extraction_contract_snapshot,
    make_extraction_arm_extractor,
)
from hl_mem.storage.claims import ClaimRepository  # noqa: E402
from hl_mem.storage.database import Database  # noqa: E402
from hl_mem.workers.discover_relations import discover_relations  # noqa: E402

SAMPLE = ROOT / "tests" / "eval" / "fixtures" / "chinese_e2e_sample.json"
DESIGN = ROOT / "tests" / "eval" / "fixtures" / "c_series_relation_design_dev.json"
MIGRATION_044 = ROOT / "src" / "hl_mem" / "storage" / "migrations" / "044_relation_bitemporal.sql"
OUTPUT_ROOT = ROOT / "var" / "eval"
CACHE_ROOT = OUTPUT_ROOT / "v028_rao_ab_cache"
INPUTS = OUTPUT_ROOT / "v028_rao_ab_inputs_nogold.json"
PREREG = OUTPUT_ROOT / "v028_rao_ab_preregistration.json"
RAW = OUTPUT_ROOT / "v028_rao_ab_recall.jsonl"
REPORT = OUTPUT_ROOT / "v028_rao_ab_report.json"
REPORT_MD = OUTPUT_ROOT / "v028_rao_ab_report.md"

EXTRACTION_ARMS = ("old", "new")
RECALL_ARMS = ("C0", "C4")
SCORER_VERSION = "answer-entity-packet-v1"
PROTOCOL_VERSION = "v028-source-bounded-rao-ab-v2"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")[:96] or "case"


def extraction_task_order(
    trajectories: Sequence[Mapping[str, Any]],
    *,
    preregistration_id: str,
) -> list[dict[str, str]]:
    """Deterministically interleave old/new cache tasks without a seed API."""
    tasks = [
        {"trajectory_id": str(item["trajectory_id"]), "extraction_arm": arm}
        for item in trajectories
        for arm in EXTRACTION_ARMS
    ]
    return sorted(
        tasks,
        key=lambda item: hashlib.sha256(
            f"{preregistration_id}|{item['trajectory_id']}|{item['extraction_arm']}".encode("utf-8")
        ).digest(),
    )


def _corpus_snapshot() -> tuple[dict[str, Path], dict[str, str]]:
    manifest = _json(SAMPLE)
    paths = {
        "chinese_e2e_manifest": SAMPLE,
        "visible_relation_design_dev": DESIGN,
        **{f"source_{key}": Path(str(value["path"])).resolve() for key, value in manifest["sources"].items()},
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"v0.28 A/B source files are missing: {missing}")
    return paths, {key: sha256_file(path) for key, path in paths.items()}


def _message_payload(message: Any) -> dict[str, Any]:
    return {
        "event_id": str(message.event_id),
        "occurred_at": str(message.occurred_at),
        "text": str(message.text),
        "place": str(message.place),
        "mid": int(message.mid),
    }


def build_gold_free_inputs() -> dict[str, Any]:
    """Load private visible sources and emit only materialization/recall inputs."""
    chinese = importlib.import_module("tests.eval.chinese_e2e")
    manifest = chinese.load_sample_manifest(SAMPLE)
    sampled = chinese.load_sampled_inputs(manifest)
    _, corpus_hashes = _corpus_snapshot()
    trajectories: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []

    for bundle in sampled.perltqa_bundles:
        trajectory = chinese.build_perltqa_ingest_trajectory(bundle)
        trajectory_id = f"perltqa:{trajectory.case_id}"
        trajectories[trajectory_id] = {
            "trajectory_id": trajectory_id,
            "namespace": trajectory.namespace,
            "events": [_message_payload(item) for item in trajectory.messages],
            "source_corpora": [
                {"id": "source_perltqa_memory", "sha256": corpus_hashes["source_perltqa_memory"]},
                {"id": "source_perltqa_qa", "sha256": corpus_hashes["source_perltqa_qa"]},
            ],
        }
        for question in bundle.questions:
            question_trajectory = chinese.build_perltqa_question_trajectory(trajectory, question)
            cases.append(
                {
                    "case_id": question.case_id,
                    "dataset": "chinese_e2e",
                    "category": f"perltqa_{question.category}",
                    "question": question.question,
                    "question_at": question_trajectory.question_at,
                    "known_as_of": None,
                    "namespace": question.namespace,
                    "trajectory_id": trajectory_id,
                    "allowed_modalities": ["text"],
                    "source_corpora": trajectories[trajectory_id]["source_corpora"],
                }
            )

    for trajectory in sampled.memdaily_trajectories:
        trajectory_id = f"memdaily:{trajectory.case_id}"
        trajectories[trajectory_id] = {
            "trajectory_id": trajectory_id,
            "namespace": trajectory.namespace,
            "events": [_message_payload(item) for item in trajectory.messages],
            "source_corpora": [{"id": "source_memdaily", "sha256": corpus_hashes["source_memdaily"]}],
        }
        cases.append(
            {
                "case_id": trajectory.case_id,
                "dataset": "chinese_e2e",
                "category": f"memdaily_{trajectory.qtype}",
                "question": trajectory.question,
                "question_at": trajectory.question_at,
                "known_as_of": None,
                "namespace": trajectory.namespace,
                "trajectory_id": trajectory_id,
                "allowed_modalities": ["text"],
                "source_corpora": trajectories[trajectory_id]["source_corpora"],
            }
        )

    for raw in _json(DESIGN)["cases"]:
        case_id = str(raw["case_id"])
        trajectory_id = f"relation_design_dev:{case_id}"
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = [
            {
                "event_id": f"{case_id}:event:{index}",
                "occurred_at": (start + timedelta(days=index - 1)).isoformat(),
                "text": str(text),
                "place": "relation_design_dev",
                "mid": index,
            }
            for index, text in enumerate(raw["events"], start=1)
        ]
        corpora = [{"id": "visible_relation_design_dev", "sha256": corpus_hashes["visible_relation_design_dev"]}]
        trajectories[trajectory_id] = {
            "trajectory_id": trajectory_id,
            "namespace": str(raw["namespace"]),
            "events": events,
            "source_corpora": corpora,
        }
        cases.append(
            {
                "case_id": case_id,
                "dataset": "relation_design_dev",
                "category": str(raw["category"]),
                "question": str(raw["question"]),
                "question_at": str(raw["question_at"]),
                "known_as_of": raw.get("known_as_of"),
                "namespace": str(raw["namespace"]),
                "trajectory_id": trajectory_id,
                "allowed_modalities": ["text"],
                "source_corpora": corpora,
            }
        )

    if len(cases) != 52:
        raise RuntimeError(f"visible A/B case count must be 52, got {len(cases)}")
    payload = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "cases": cases,
        "trajectories": [trajectories[key] for key in sorted(trajectories)],
    }
    assert_gold_free(payload)
    return payload


def _settings_snapshot(settings: Any) -> dict[str, Any]:
    return {
        "provider": settings.llm_provider,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "temperature": "provider_default_omitted",
        "structured_mode": settings.llm_structured_mode,
        "schema_retries": settings.llm_schema_retries,
        "timeout_seconds": settings.llm_timeout,
        "max_attempts": settings.llm_max_attempts,
        "verification_mode": settings.verification_mode,
        "chunk_target_chars": settings.extraction_chunk_target_chars,
        "chunk_overlap_turns": settings.extraction_chunk_overlap_turns,
        "max_split_depth": settings.extraction_max_split_depth,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "relation_discovery": {
            "mode": "auto",
            "source_selector": "gold-free C0 Top-5 seed union per trajectory",
            "pool_limit": settings.relation_discovery_pool_limit,
            "max_proposals": settings.relation_discovery_max_proposals,
            "auto_apply_confidence": settings.relation_auto_apply_confidence,
            "conflict_confidence": settings.relation_conflict_confidence,
            "valid_time_replay_rule": "max(endpoint.valid_from); synthetic historical replay only",
        },
    }


def _implementation_snapshot() -> dict[str, str]:
    paths = {
        "runner": Path(__file__),
        "contract_adapter": ROOT / "src" / "hl_mem" / "evaluation" / "extraction_ab.py",
        "extractor": ROOT / "src" / "hl_mem" / "ingest" / "llm_extractor.py",
        "schema": ROOT / "src" / "hl_mem" / "ingest" / "schemas.py",
        "runtime": ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        "scorer": ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
        "relation_discovery": ROOT / "src" / "hl_mem" / "workers" / "discover_relations.py",
        "migration_044": MIGRATION_044,
    }
    return {key: sha256_file(path) for key, path in paths.items()}


def command_preregister() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    if "coding" not in settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("v0.28 extraction A/B requires the configured coding-plan endpoint and key")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked source must be clean before preregistration")
    inputs = build_gold_free_inputs()
    write_json_atomic(INPUTS, inputs)
    commit = _git("rev-parse", "HEAD")
    preregistration_id = f"v028-rao-ab-{commit[:12]}"
    _, corpus_hashes = _corpus_snapshot()
    tasks = extraction_task_order(inputs["trajectories"], preregistration_id=preregistration_id)
    manifest = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "preregistration_id": preregistration_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "case_count": len(inputs["cases"]),
        "trajectory_count": len(inputs["trajectories"]),
        "logical_cache_units": len(tasks),
        "repeat_count": 1,
        "task_order_sha256": _canonical_hash(tasks),
        "inputs_sha256": sha256_file(INPUTS),
        "corpora": corpus_hashes,
        "settings": _settings_snapshot(settings),
        "contracts": {arm: extraction_contract_snapshot(arm) for arm in EXTRACTION_ARMS},
        "scorer_version": SCORER_VERSION,
        "implementation_snapshot": _implementation_snapshot(),
        "metrics": [
            "exact_rao_rate",
            "source_bounded_rao_rate",
            "claim_yield_per_event",
            "nonrelation_claim_yield_per_event",
            "canonical_slot_mismatch_rate",
            "packet_rao_completeness",
            "legacy_anchor_coverage",
            "entity_coverage_at_5",
            "forbidden_modality_provenance_leakage",
        ],
        "release_gates": {
            "source_bounded_rao": "new >= 0.80 and new > old",
            "exact_rao": "new > old",
            "packet_rao": "new > old",
            "entity_coverage": "new > old",
            "nonrelation_yield": "new >= 95% of old",
            "canonical_slot": "new mismatch rate <= old",
            "legacy_anchors": "new >= old",
            "safety": "forbidden/modality/provenance/leakage all zero",
            "relation_coverage": "every gold-declared relation case has at least one visible edge",
            "packet_smoke": "three deterministic relation cases have C0 != C4",
        },
        "retry_policy": "provider max_attempts=3; retry same arm/model/prompt/schema/cache unit",
        "cost_bound": {
            "extraction_calls_expected": "280-340",
            "relation_discovery_calls_maximum": 5 * len(inputs["cases"]) * len(EXTRACTION_ARMS),
            "relation_discovery_source_selector": "gold-free C0 Top-5 seed union; no gold fields loaded",
        },
        "sealed_v3": "not_loaded_or_run",
    }
    write_json_atomic(PREREG, manifest)
    print(json.dumps({"preregistration": str(PREREG), "cases": 52, "cache_units": len(tasks)}))
    return 0


def _verify_preregistration(settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _json(PREREG)
    inputs = _json(INPUTS)
    if manifest.get("protocol_version") != PROTOCOL_VERSION or manifest.get("scorer_version") != SCORER_VERSION:
        raise RuntimeError("v0.28 extraction A/B protocol/scorer drift")
    if _git("rev-parse", "HEAD") != manifest.get("git_commit"):
        raise RuntimeError("git commit differs from preregistration")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked source changed after preregistration")
    if sha256_file(INPUTS) != manifest.get("inputs_sha256"):
        raise RuntimeError("gold-free input snapshot drift")
    if manifest.get("settings") != _settings_snapshot(settings):
        raise RuntimeError("model/extraction/relation settings drift")
    if manifest.get("contracts") != {arm: extraction_contract_snapshot(arm) for arm in EXTRACTION_ARMS}:
        raise RuntimeError("old/new prompt or schema drift")
    if manifest.get("implementation_snapshot") != _implementation_snapshot():
        raise RuntimeError("A/B implementation snapshot drift")
    _, corpora = _corpus_snapshot()
    if manifest.get("corpora") != corpora:
        raise RuntimeError("visible corpus snapshot drift")
    assert_gold_free(inputs)
    return manifest, inputs


def _cache_paths(trajectory_id: str, arm: str) -> tuple[Path, Path]:
    root = (CACHE_ROOT / arm).resolve()
    database = (root / f"{_safe_name(trajectory_id)}.db").resolve()
    manifest = database.with_suffix(".manifest.json")
    if not database.is_relative_to(root) or manifest.parent != root:
        raise RuntimeError("A/B cache path escaped its arm root")
    return database, manifest


def _remove_cache_artifacts(database: Path, manifest: Path) -> None:
    root = CACHE_ROOT.resolve()
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm"), manifest):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved.parent != database.parent:
            raise RuntimeError("refusing to remove cache artifact outside A/B cache root")
        resolved.unlink(missing_ok=True)


def _cache_fingerprint(trajectory: Mapping[str, Any], arm: str, prereg: Mapping[str, Any]) -> str:
    return _canonical_hash(
        {
            "trajectory": trajectory,
            "contract": prereg["contracts"][arm],
            "settings": prereg["settings"],
            "implementation": prereg["implementation_snapshot"],
        }
    )


def _valid_cache(database: Path, manifest_path: Path, fingerprint: str) -> bool:
    if not database.is_file() or not manifest_path.is_file():
        return False
    try:
        manifest = _json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return manifest.get("fingerprint") == fingerprint and manifest.get("db_sha256") == sha256_file(database)


def relation_discovery_seed_ids(
    cases: Sequence[Mapping[str, Any]],
    *,
    settings: Any,
    embedder: Any,
    reranker: Any,
    db_path: Path,
) -> list[str]:
    """Select the bounded, gold-free union of each visible case's C0 Top-5 seeds."""
    selected: dict[str, None] = {}
    for case in cases:
        execution = recall_visible_case(
            case,
            settings,
            embedder,
            reranker,
            db_path=db_path,
            arm_id="C0",
        )
        if len(execution.seed_packet) > 5:
            raise RuntimeError("C0 relation discovery source packet exceeded frozen Top-5 bound")
        for item in execution.seed_packet:
            claim_id = item.get("claim_id")
            if claim_id:
                selected.setdefault(str(claim_id), None)
    if len(selected) > 5 * len(cases):
        raise RuntimeError("relation discovery source union exceeded frozen Top-5 bound")
    return list(selected)


def _ingest_cache(
    database_path: Path,
    trajectory: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    arm: str,
    settings: Any,
    embedder: Any,
    reranker: Any,
) -> dict[str, Any]:
    runtime = dataclasses.replace(settings, relation_discovery_mode="auto", query_expansion_mode="off")
    runtime.validate()
    database = Database(database_path, settings=runtime)
    counts: Counter[str] = Counter()
    relation_counts: Counter[str] = Counter()
    try:
        with database.connect() as connection:
            service = IngestService(connection)
            extractor = make_extraction_arm_extractor(runtime, connection, arm)  # type: ignore[arg-type]
            for raw in trajectory["events"]:
                event = {
                    "id": str(raw["event_id"]),
                    "idempotency_key": f"v028-rao-ab:{arm}:{trajectory['trajectory_id']}:{raw['event_id']}",
                    "tenant_id": str(trajectory["namespace"]),
                    "event_type": "message",
                    "actor_type": "user",
                    "content": {
                        "text": str(raw["text"]),
                        "benchmark_locator": {
                            "trajectory_id": str(trajectory["trajectory_id"]),
                            "event_id": str(raw["event_id"]),
                        },
                    },
                    "occurred_at": str(raw["occurred_at"]),
                    "recorded_at": str(raw["occurred_at"]),
                }
                service.ingest_event(event)
                event["extractor"] = "llm"
                event["extractor_version"] = extractor.extractor_version
                claims = extractor.extract(
                    event["content"],
                    {
                        "actor_type": "user",
                        "event_type": "message",
                        "occurred_at": event["occurred_at"],
                    },
                )
                counts["events"] += 1
                counts["claims_extracted"] += len(claims)
                counts["extraction_calls"] += extractor.last_llm_call_count
                counts["input_tokens"] += extractor.last_input_tokens
                counts["output_tokens"] += extractor.last_output_tokens
                counts["total_tokens"] += extractor.last_usage_tokens
                for reason, value in extractor.last_relation_metadata.items():
                    counts[f"relation_metadata:{reason}"] += value
                now = datetime.now(timezone.utc).isoformat()
                for claim in claims:
                    result = IngestService.store_extracted(
                        connection,
                        claim,
                        event,
                        now,
                        embedder,
                        policy=runtime.retention_policy(),
                        relation_discovery_mode="off",
                        index_text_mode=runtime.index_text_mode,
                    )
                    counts["claims_skipped" if result.status == "skipped" else "claims_stored"] += 1

        seed_ids = relation_discovery_seed_ids(
            cases,
            settings=runtime,
            embedder=embedder,
            reranker=reranker,
            db_path=database_path,
        )
        with database.connect() as connection:
            discoverer = make_relation_discoverer(runtime, connection)
            if discoverer is None:
                raise RuntimeError("A/B cache requires relation discovery=auto")
            for claim_id in seed_ids:
                relation_counts.update(
                    discover_relations(
                        connection,
                        discoverer,
                        claim_id,
                        mode="auto",
                        pool_limit=runtime.relation_discovery_pool_limit,
                        max_proposals=runtime.relation_discovery_max_proposals,
                        auto_apply_confidence=runtime.relation_auto_apply_confidence,
                        conflict_confidence=runtime.relation_conflict_confidence,
                    )
                )
            # These fixtures replay historical events. Production creates an edge at
            # wall-clock now; the replay clock instead starts it when both endpoint
            # facts have become valid, preserving M3 filtering at question_at.
            connection.execute(
                "UPDATE memory_relations SET valid_from=("
                "SELECT MAX(COALESCE(source.valid_from,source.recorded_from),"
                "COALESCE(target.valid_from,target.recorded_from)) "
                "FROM claims source JOIN claims target ON target.id=memory_relations.to_id "
                "WHERE source.id=memory_relations.from_id)"
            )
            connection.commit()
            counts["relation_discovery_calls"] = len(seed_ids)
            counts["relation_discovery_call_bound"] = 5 * len(cases)
            counts["relations_visible"] = int(
                connection.execute("SELECT COUNT(*) FROM memory_relations WHERE valid_to IS NULL").fetchone()[0]
            )
            counts["nonrelation_claims"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM claims WHERE "
                    "json_extract(qualifiers_json,'$.role') IS NULL OR "
                    "json_extract(qualifiers_json,'$.action') IS NULL OR "
                    "json_extract(qualifiers_json,'$.object') IS NULL"
                ).fetchone()[0]
            )
    finally:
        database.close()
    return {**dict(sorted(counts.items())), "relation_discovery": dict(sorted(relation_counts.items()))}


def command_prepare_cache() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, inputs = _verify_preregistration(settings)
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    trajectories = {str(item["trajectory_id"]): item for item in inputs["trajectories"]}
    cases_by_trajectory: dict[str, list[dict[str, Any]]] = {}
    for case in inputs["cases"]:
        cases_by_trajectory.setdefault(str(case["trajectory_id"]), []).append(case)
    tasks = extraction_task_order(inputs["trajectories"], preregistration_id=prereg["preregistration_id"])
    if _canonical_hash(tasks) != prereg.get("task_order_sha256"):
        raise RuntimeError("extraction task order drift")
    built = reused = 0
    for index, task in enumerate(tasks, start=1):
        trajectory = trajectories[task["trajectory_id"]]
        arm = task["extraction_arm"]
        database, manifest_path = _cache_paths(task["trajectory_id"], arm)
        database.parent.mkdir(parents=True, exist_ok=True)
        fingerprint = _cache_fingerprint(trajectory, arm, prereg)
        if _valid_cache(database, manifest_path, fingerprint):
            reused += 1
            continue
        _remove_cache_artifacts(database, manifest_path)
        print(f"[{index}/{len(tasks)}] extract {arm} {task['trajectory_id']}", flush=True)
        stats = _ingest_cache(
            database,
            trajectory,
            cases_by_trajectory[task["trajectory_id"]],
            arm,
            settings,
            embedder,
            reranker,
        )
        payload = {
            "schema_version": 1,
            "trajectory_id": task["trajectory_id"],
            "extraction_arm": arm,
            "fingerprint": fingerprint,
            "contract": prereg["contracts"][arm],
            "db_sha256": sha256_file(database),
            "stats": stats,
        }
        write_json_atomic(manifest_path, payload)
        built += 1
    print(json.dumps({"cache_units": len(tasks), "built": built, "reused": reused}))
    return 0


def _cache_index(prereg: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for trajectory in inputs["trajectories"]:
        trajectory_id = str(trajectory["trajectory_id"])
        for arm in EXTRACTION_ARMS:
            database, manifest_path = _cache_paths(trajectory_id, arm)
            fingerprint = _cache_fingerprint(trajectory, arm, prereg)
            if not _valid_cache(database, manifest_path, fingerprint):
                raise RuntimeError(f"missing or stale A/B cache: {trajectory_id}/{arm}")
            result[(trajectory_id, arm)] = {"database": database, "manifest": _json(manifest_path)}
    return result


def command_recall() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, inputs = _verify_preregistration(settings)
    caches = _cache_index(prereg, inputs)
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    rows: list[dict[str, Any]] = []
    for case in inputs["cases"]:
        for extraction_arm in EXTRACTION_ARMS:
            cache = caches[(str(case["trajectory_id"]), extraction_arm)]
            database = cache["database"]
            runtime_case = {
                **case,
                "source_cache_identity": str(database.resolve()),
                "source_cache_sha256": sha256_file(database),
            }
            assert_gold_free(runtime_case)
            for recall_arm in RECALL_ARMS:
                result = recall_visible_case(
                    runtime_case,
                    settings,
                    embedder,
                    reranker,
                    db_path=database,
                    arm_id=recall_arm,
                )
                rows.append(
                    {
                        "case_id": case["case_id"],
                        "dataset": case["dataset"],
                        "category": case["category"],
                        "trajectory_id": case["trajectory_id"],
                        "extraction_arm": extraction_arm,
                        "recall_arm": recall_arm,
                        "packet": list(result.packet),
                        "top5_seed_packet": list(result.seed_packet),
                        "answerability": result.answerability,
                        "relation_paths": list(result.relation_paths),
                        "recall_latency_seconds": result.recall_latency_seconds,
                    }
                )
    assert_gold_free(rows)
    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "raw": str(RAW)}))
    return 0


def _gold_index() -> dict[str, dict[str, Any]]:
    chinese = importlib.import_module("tests.eval.chinese_e2e")
    manifest = chinese.load_sample_manifest(SAMPLE)
    sampled = chinese.load_sampled_inputs(manifest)
    result: dict[str, dict[str, Any]] = {}
    for bundle in sampled.perltqa_bundles:
        for question in bundle.questions:
            result[question.case_id] = {
                "gold": asdict(question.answer_entity_gold),
                "legacy_anchors": list(question.answer_anchors),
                "accepted_rubrics": [
                    [list(expression) for expression in rubric] for rubric in question.accepted_rubrics
                ],
            }
    for trajectory in sampled.memdaily_trajectories:
        result[trajectory.case_id] = {
            "gold": asdict(manifest.answer_entity_gold_by_case_id[trajectory.case_id]),
            "legacy_anchors": [],
            "accepted_rubrics": [],
        }
    for raw in _json(DESIGN)["cases"]:
        result[str(raw["case_id"])] = {
            "gold": dict(raw["gold"]),
            "legacy_anchors": [],
            "accepted_rubrics": [],
        }
    if len(result) != 52:
        raise RuntimeError(f"visible scoring gold count must be 52, got {len(result)}")
    return result


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _all_claims(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        claims: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM claims ORDER BY id").fetchall():
            claim = ClaimRepository._decode_claim(dict(row))
            if claim is not None:
                claims.append(claim)
        return claims
    finally:
        connection.close()


def _exact_rao_match(claims: Sequence[Mapping[str, Any]], gold: Mapping[str, Any]) -> bool | None:
    expected = {
        (_nfc(item["role"]), _nfc(item["action"]), _nfc(item["object"]))
        for item in gold.get("role_action_object") or []
    }
    if not expected:
        return None
    actual = {
        (_nfc(qualifiers.get("role")), _nfc(qualifiers.get("action")), _nfc(qualifiers.get("object")))
        for claim in claims
        if isinstance((qualifiers := claim.get("qualifiers")), Mapping)
        and qualifiers.get("role")
        and qualifiers.get("action")
        and qualifiers.get("object")
    }
    return expected.issubset(actual)


def _canonical_slot_mismatches(database: Path) -> tuple[int, int]:
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    mismatches = total = 0
    try:
        repository = ClaimRepository(connection)
        for row in connection.execute("SELECT id FROM claims ORDER BY id").fetchall():
            claim = repository.get_claim(str(row["id"]))
            if claim is None:
                continue
            slot = SLOT_REGISTRY.get(str(claim.get("canonical_slot") or ""))
            qualifiers = claim.get("qualifiers")
            if slot is None or not slot.required_qualifiers or not isinstance(qualifiers, Mapping):
                continue
            evidence_rows = connection.execute(
                "SELECT e.content_json FROM evidence_links l JOIN events e ON e.id=l.evidence_id "
                "WHERE l.derived_type='claim' AND l.derived_id=? AND l.evidence_type='event'",
                (claim["id"],),
            ).fetchall()
            evidence = " ".join(_nfc(row["content_json"]) for row in evidence_rows)
            value = _nfc(claim.get("value"))
            total += 1
            if any(
                not (needle := _nfc(qualifiers.get(key))) or needle not in value or needle not in evidence
                for key in slot.required_qualifiers
            ):
                mismatches += 1
    finally:
        connection.close()
    return mismatches, total


def _mean(values: Sequence[float]) -> float:
    return fmean(values) if values else 0.0


def _arm_metrics(
    extraction_arm: str,
    rows: Mapping[tuple[str, str, str], Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    gold: Mapping[str, Mapping[str, Any]],
    caches: Mapping[tuple[str, str], Mapping[str, Any]],
    prereg: Mapping[str, Any],
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    unique_caches = [value for (trajectory_id, arm), value in caches.items() if arm == extraction_arm]
    cache_stats = [item["manifest"]["stats"] for item in unique_caches]
    events = sum(int(item.get("events", 0)) for item in cache_stats)
    claims = sum(int(item.get("claims_stored", 0)) for item in cache_stats)
    nonrelation_claims = sum(int(item.get("nonrelation_claims", 0)) for item in cache_stats)
    accepted = sum(int(item.get("relation_metadata:accepted", 0)) for item in cache_stats)
    discarded = sum(
        int(value)
        for item in cache_stats
        for key, value in item.items()
        if str(key).startswith("relation_metadata:") and key != "relation_metadata:accepted"
    )
    cache_files = {str(item["database"].resolve()): sha256_file(item["database"]) for item in unique_caches}
    provenance_manifest = {"cache_files": cache_files, "corpora": prereg["corpora"]}

    exact: list[float] = []
    canonical_mismatches = canonical_total = 0
    relation_required = relation_covered = 0
    claims_by_trajectory = {
        trajectory_id: _all_claims(caches[(trajectory_id, extraction_arm)]["database"])
        for trajectory_id in {str(case["trajectory_id"]) for case in cases}
    }
    for trajectory_id in claims_by_trajectory:
        mismatch, total = _canonical_slot_mismatches(caches[(trajectory_id, extraction_arm)]["database"])
        canonical_mismatches += mismatch
        canonical_total += total

    per_recall: dict[str, dict[str, Any]] = {}
    for recall_arm in RECALL_ARMS:
        entity_coverages: list[float] = []
        packet_rao: list[float] = []
        anchor_scores: list[float] = []
        violations: Counter[str] = Counter()
        for case in cases:
            case_id = str(case["case_id"])
            row = rows[(case_id, extraction_arm, recall_arm)]
            packet = row["packet"]
            gold_item = gold[case_id]
            scored = frozen_scorer.score_visible_case("", packet, gold_item["gold"])
            coverage = scored.get("entity_coverage_at_5")
            if coverage is not None:
                entity_coverages.append(float(coverage))
            if gold_item["gold"].get("role_action_object"):
                packet_rao.append(float(bool(scored["packet_rao_match"])))
            if gold_item["legacy_anchors"]:
                anchor_scores.append(
                    float(
                        importlib.import_module("tests.eval.chinese_e2e").score_answer(
                            render_packet_context(packet),
                            gold_item["legacy_anchors"],
                            gold_item["accepted_rubrics"],
                        )["answer_correct"]
                    )
                )
            violations["forbidden"] += int(bool(scored["negative_violation"]))
            database = caches[(str(case["trajectory_id"]), extraction_arm)]["database"]
            runtime_case = {
                **case,
                "source_cache_identity": str(database.resolve()),
                "source_cache_sha256": sha256_file(database),
            }
            audit = frozen_scorer.audit_evidence_provenance(packet, runtime_case, provenance_manifest)
            violations["modality"] += int(bool(audit["modality"]))
            violations["provenance"] += int(bool(audit["provenance"]))
        per_recall[recall_arm] = {
            "entity_coverage_at_5": _mean(entity_coverages),
            "entity_coverage_cases": len(entity_coverages),
            "packet_rao_completeness": _mean(packet_rao),
            "packet_rao_cases": len(packet_rao),
            "legacy_anchor_coverage": _mean(anchor_scores),
            "legacy_anchor_cases": len(anchor_scores),
            "forbidden_violations": violations["forbidden"],
            "modality_violations": violations["modality"],
            "provenance_violations": violations["provenance"],
        }

    for case in cases:
        case_id = str(case["case_id"])
        exact_match = _exact_rao_match(claims_by_trajectory[str(case["trajectory_id"])], gold[case_id]["gold"])
        if exact_match is not None:
            exact.append(float(exact_match))
            relation_required += 1
            if rows[(case_id, extraction_arm, "C4")].get("relation_paths"):
                relation_covered += 1

    c4 = per_recall["C4"]
    leakage = int(bool(frozen_scorer.audit_leakage(inputs))) + int(
        bool(frozen_scorer.audit_leakage([row for key, row in rows.items() if key[1] == extraction_arm]))
    )
    return {
        "source_bounded_rao_rate": accepted / (accepted + discarded) if accepted + discarded else 0.0,
        "source_bounded_rao_accepted": accepted,
        "source_bounded_rao_discarded": discarded,
        "exact_rao_rate": _mean(exact),
        "exact_rao_cases": len(exact),
        "claim_yield_per_event": claims / events if events else 0.0,
        "nonrelation_claim_yield_per_event": nonrelation_claims / events if events else 0.0,
        "events": events,
        "claims_stored": claims,
        "canonical_slot_mismatch_rate": canonical_mismatches / canonical_total if canonical_total else 0.0,
        "canonical_slot_mismatches": canonical_mismatches,
        "canonical_slot_cases": canonical_total,
        "packet_rao_completeness": c4["packet_rao_completeness"],
        "entity_coverage_at_5": c4["entity_coverage_at_5"],
        "legacy_anchor_coverage": c4["legacy_anchor_coverage"],
        "forbidden_violations": c4["forbidden_violations"],
        "modality_violations": c4["modality_violations"],
        "provenance_violations": c4["provenance_violations"],
        "leakage_violations": leakage,
        "relation_required_cases": relation_required,
        "relation_covered_cases": relation_covered,
        "extraction_calls": sum(int(item.get("extraction_calls", 0)) for item in cache_stats),
        "relation_discovery_calls": sum(int(item.get("relation_discovery_calls", 0)) for item in cache_stats),
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in cache_stats),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in cache_stats),
        "total_tokens": sum(int(item.get("total_tokens", 0)) for item in cache_stats),
        "recall_arms": per_recall,
    }


def evaluate_release_gates(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    *,
    relation_coverage_passed: bool,
    packet_smoke_passed: bool,
) -> dict[str, Any]:
    """Apply only the preregistered first-stage release gates."""
    checks = [
        {
            "id": "source_bounded_rao_net_gain",
            "passed": float(new["source_bounded_rao_rate"]) >= 0.80
            and float(new["source_bounded_rao_rate"]) > float(old["source_bounded_rao_rate"]),
        },
        {"id": "exact_rao_net_gain", "passed": float(new["exact_rao_rate"]) > float(old["exact_rao_rate"])},
        {
            "id": "packet_rao_net_gain",
            "passed": float(new["packet_rao_completeness"]) > float(old["packet_rao_completeness"]),
        },
        {
            "id": "entity_coverage_net_gain",
            "passed": float(new["entity_coverage_at_5"]) > float(old["entity_coverage_at_5"]),
        },
        {
            "id": "nonrelation_claim_yield_preserved",
            "passed": float(new["nonrelation_claim_yield_per_event"])
            >= 0.95 * float(old["nonrelation_claim_yield_per_event"]),
        },
        {
            "id": "canonical_slot_no_regression",
            "passed": float(new["canonical_slot_mismatch_rate"]) <= float(old["canonical_slot_mismatch_rate"]),
        },
        {
            "id": "legacy_anchor_no_regression",
            "passed": float(new["legacy_anchor_coverage"]) >= float(old["legacy_anchor_coverage"]),
        },
        {
            "id": "safety_zero_violations",
            "passed": all(
                int(new[key]) == 0
                for key in (
                    "forbidden_violations",
                    "modality_violations",
                    "provenance_violations",
                    "leakage_violations",
                )
            ),
        },
        {"id": "relation_coverage", "passed": relation_coverage_passed},
        {"id": "c0_c4_packet_smoke", "passed": packet_smoke_passed},
    ]
    return {"passed": all(bool(item["passed"]) for item in checks), "checks": checks}


def command_score() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    prereg, inputs = _verify_preregistration(settings)
    caches = _cache_index(prereg, inputs)
    raw_rows = _read_jsonl(RAW)
    if len(raw_rows) != 52 * len(EXTRACTION_ARMS) * len(RECALL_ARMS):
        raise RuntimeError(f"A/B recall row count mismatch: {len(raw_rows)}")
    assert_gold_free(raw_rows)
    rows = {(str(row["case_id"]), str(row["extraction_arm"]), str(row["recall_arm"])): row for row in raw_rows}
    if len(rows) != len(raw_rows):
        raise RuntimeError("duplicate A/B recall rows")
    gold = _gold_index()
    metrics = {arm: _arm_metrics(arm, rows, inputs["cases"], gold, caches, prereg, inputs) for arm in EXTRACTION_ARMS}
    required_cases = [
        str(case["case_id"]) for case in inputs["cases"] if gold[str(case["case_id"])]["gold"].get("role_action_object")
    ]
    sample = sorted(
        required_cases,
        key=lambda case_id: hashlib.sha256(f"{prereg['preregistration_id']}|{case_id}".encode()).digest(),
    )[:3]
    smoke_details: dict[str, dict[str, str | bool]] = {}
    for case_id in sample:
        c0 = rows[(case_id, "new", "C0")]["packet"]
        c4 = rows[(case_id, "new", "C4")]["packet"]
        smoke_details[case_id] = {
            "C0": _canonical_hash(c0),
            "C4": _canonical_hash(c4),
            "different": _canonical_hash(c0) != _canonical_hash(c4),
        }
    packet_smoke_passed = len(smoke_details) == 3 and all(bool(item["different"]) for item in smoke_details.values())
    relation_coverage_passed = (
        metrics["new"]["relation_required_cases"] > 0
        and metrics["new"]["relation_covered_cases"] == metrics["new"]["relation_required_cases"]
    )
    gates = evaluate_release_gates(
        metrics["old"],
        metrics["new"],
        relation_coverage_passed=relation_coverage_passed,
        packet_smoke_passed=packet_smoke_passed,
    )
    report = {
        "schema_version": 1,
        "protocol_version": PROTOCOL_VERSION,
        "scorer_version": SCORER_VERSION,
        "preregistration_sha256": sha256_file(PREREG),
        "raw_sha256": sha256_file(RAW),
        "metrics": metrics,
        "structural_gates": {
            "relation_coverage_passed": relation_coverage_passed,
            "packet_smoke_passed": packet_smoke_passed,
            "packet_smoke": smoke_details,
        },
        "release_gates": gates,
        "sealed_v3": "not_run",
    }
    write_json_atomic(REPORT, report)
    lines = [
        "# v0.28 compact RAO extraction A/B",
        "",
        "| arm | exact RAO | source-bounded | claim/event | slot mismatch | packet RAO | entity@5 | anchors | violations | extract calls |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in EXTRACTION_ARMS:
        item = metrics[arm]
        violations = sum(
            int(item[key])
            for key in ("forbidden_violations", "modality_violations", "provenance_violations", "leakage_violations")
        )
        lines.append(
            f"| {arm} | {item['exact_rao_rate']:.4f} | {item['source_bounded_rao_rate']:.4f} | "
            f"{item['claim_yield_per_event']:.4f} | {item['canonical_slot_mismatch_rate']:.4f} | "
            f"{item['packet_rao_completeness']:.4f} | {item['entity_coverage_at_5']:.4f} | "
            f"{item['legacy_anchor_coverage']:.4f} | {violations} | {item['extraction_calls']} |"
        )
    lines.extend(["", f"Release gate: {'PASS' if gates['passed'] else 'FAIL'}"])
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "gate_passed": gates["passed"]}))
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preregister", "prepare-cache", "recall", "score"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    command = parse_args(argv).command
    if command == "preregister":
        return command_preregister()
    if command == "prepare-cache":
        return command_prepare_cache()
    if command == "recall":
        return command_recall()
    return command_score()


if __name__ == "__main__":
    raise SystemExit(main())
