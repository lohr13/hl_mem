#!/usr/bin/env python
"""Run the visible 52-case packet-RAO representation validation.

The live command reads only gold-free inputs and frozen packet snapshots. Gold
is loaded only by the offline ``score`` command.
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
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.tools import run_c_series_relation_experiment as base  # noqa: E402
from evaluation.tools import score_c_series_relation_experiment as scorer  # noqa: E402
from hl_mem.application.context_packet import render_memory_text  # noqa: E402
from hl_mem.application.recall import _claim_relation  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.evaluation.c_series import (  # noqa: E402
    is_retryable_error,
    sha256_file,
    write_json_atomic,
)
from hl_mem.evaluation.c_series_runtime import (  # noqa: E402
    assert_gold_free,
    render_packet_context,
)
from hl_mem.storage.claims import ClaimRepository  # noqa: E402

INPUTS = ROOT / "var" / "eval" / "c_series_inputs_nogold.json"
SOURCE_RAW = ROOT / "var" / "eval" / "c_series_raw.jsonl"
SOURCE_REPORT = ROOT / "var" / "eval" / "c_series_report.json"
SOURCE_READER_AB = ROOT / "var" / "eval" / "reader_ab_glm53_results.jsonl"
SOURCE_READER_SUMMARY = ROOT / "var" / "eval" / "reader_ab_summary.json"
SAMPLE = ROOT / "tests" / "eval" / "fixtures" / "chinese_e2e_sample.json"
DESIGN = ROOT / "tests" / "eval" / "fixtures" / "c_series_relation_design_dev.json"
PACKETS = ROOT / "var" / "eval" / "packet_rao_design_dev_packets.json"
PREREG = ROOT / "var" / "eval" / "packet_rao_design_dev_preregistration.json"
RAW = ROOT / "var" / "eval" / "packet_rao_design_dev_raw.jsonl"
REPORT = ROOT / "var" / "eval" / "packet_rao_design_dev_report.json"
REPORT_MD = ROOT / "var" / "eval" / "packet_rao_design_dev_report.md"

ARMS = ("C0", "C4")
READERS = ("qwen", "glm")
GLM_KEY_ENV = "C_SERIES_ZHIPU_API_KEY"
GLM_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
GLM_MODEL = "glm-5.3"
IMPLEMENTATION_VERSION = "packet-rao-design-dev-v1"


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


def build_tasks(
    cases: Sequence[Mapping[str, Any]],
    *,
    preregistration_id: str,
) -> list[dict[str, str]]:
    """Return one deterministically interleaved task per case/arm/reader cell."""

    tasks = [
        {"case_id": str(case["case_id"]), "arm_id": arm, "reader_id": reader}
        for case in cases
        for arm in ARMS
        for reader in READERS
    ]
    return sorted(
        tasks,
        key=lambda item: hashlib.sha256(
            (f"{preregistration_id}|{item['case_id']}|{item['arm_id']}|{item['reader_id']}").encode("utf-8")
        ).digest(),
    )


def upgrade_packet(packet: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Apply production RAO rendering and the unchanged ten-claim/2k-token limits."""

    upgraded: list[dict[str, Any]] = []
    used = 0
    for raw in packet:
        item = dict(raw)
        text = str(item.get("text") or "")
        rendered = render_memory_text(
            text,
            role=item.get("role"),
            action=item.get("action"),
            object_=item.get("object"),
        )
        tokens = max(1, (len(rendered) + 1) // 2)
        if len(upgraded) >= 10 or used + tokens > 2_000:
            continue
        item["rendered_text"] = rendered
        item["token_count"] = tokens
        upgraded.append(item)
        used += tokens
    return upgraded


def enrich_packet_relations(
    packet: Sequence[Mapping[str, Any]],
    claims_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Fill missing experimental metadata from the production claim projection."""

    enriched: list[dict[str, Any]] = []
    for raw in packet:
        item = dict(raw)
        if not (item.get("role") and item.get("action") and item.get("object")):
            claim = claims_by_id.get(str(item.get("claim_id") or ""))
            relation = _claim_relation(claim) if claim is not None else None
            if relation is not None:
                item.update(zip(("role", "action", "object"), relation, strict=True))
        enriched.append(item)
    return enriched


def legacy_packet(packet: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project the pre-v1.1 product representation without mutating the snapshot."""

    return [
        {key: value for key, value in item.items() if key not in {"role", "action", "object", "rendered_text"}}
        for item in packet
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _source_packets(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = {
        (str(row["case_id"]), int(row["repeat_index"]), str(row["arm_id"])): row
        for row in _read_jsonl(SOURCE_RAW)
        if row.get("status") == "complete" and str(row.get("arm_id")) in ARMS
    }
    snapshots: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["case_id"])
        source_rows = [rows.get((case_id, 0, arm)) for arm in ARMS]
        claim_ids = list(
            dict.fromkeys(
                str(item.get("claim_id") or "")
                for source in source_rows
                if source is not None
                for field in ("packet", "top5_seed_packet")
                for item in source.get(field) or []
                if item.get("claim_id") and not str(item["claim_id"]).startswith("raw:")
            )
        )
        database = Path(str(case["db_path"]))
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            claims_by_id = ClaimRepository(connection).batch_get_claims(claim_ids)
        finally:
            connection.close()
        for arm in ARMS:
            source = rows.get((case_id, 0, arm))
            if source is None:
                raise RuntimeError(f"old packet source missing: {case_id}/{arm}")
            packet = upgrade_packet(enrich_packet_relations(source.get("packet") or [], claims_by_id))
            seed_packet = upgrade_packet(enrich_packet_relations(source.get("top5_seed_packet") or [], claims_by_id))
            snapshots.append(
                {
                    "packet_key": f"{case_id}|{arm}",
                    "case_id": case_id,
                    "arm_id": arm,
                    "packet": packet,
                    "legacy_packet": legacy_packet(packet),
                    "top5_seed_packet": seed_packet,
                    "answerability": str(source.get("answerability") or "no_evidence"),
                    "source_repeat_index": 0,
                    "source_recall_latency_seconds": float(source.get("recall_latency_seconds") or 0.0),
                }
            )
    return snapshots


def _implementation_snapshot() -> dict[str, str]:
    files = {
        "runner": Path(__file__),
        "runtime": ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        "packet": ROOT / "src" / "hl_mem" / "application" / "context_packet.py",
        "base_runner": ROOT / "evaluation" / "tools" / "run_c_series_relation_experiment.py",
        "frozen_scorer": ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
    }
    return {
        "version": IMPLEMENTATION_VERSION,
        **{f"{name}_sha256": sha256_file(path) for name, path in files.items()},
    }


def command_preregister() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    if "coding" not in settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("qwen coding-plan endpoint/key required")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked source must be clean before preregistration")
    inputs = _json(INPUTS)
    cases = inputs.get("cases") or []
    if len(cases) != 52:
        raise RuntimeError(f"visible design/dev input count must be 52, got {len(cases)}")
    assert_gold_free(inputs)
    packets = {
        "schema_version": 1,
        "representation": "context-packet-v1.1-rao",
        "packets": _source_packets(cases),
    }
    assert_gold_free(packets)
    write_json_atomic(PACKETS, packets)
    commit = _git("rev-parse", "HEAD")
    preregistration_id = f"packet-rao-design-dev-{commit[:12]}"
    tasks = build_tasks(cases, preregistration_id=preregistration_id)
    manifest = {
        "schema_version": 1,
        "preregistration_id": preregistration_id,
        "git_commit": commit,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scorer_version": "answer-entity-packet-v1",
        "representation": "context-packet-v1.1-rao",
        "case_count": len(cases),
        "task_count": len(tasks),
        "arms": list(ARMS),
        "readers": {
            "qwen": {
                "provider": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "model": settings.llm_model,
                "temperature": 0.1,
                "max_output_tokens": 512,
            },
            "glm": {
                "provider": "zhipu",
                "base_url": GLM_BASE_URL,
                "model": GLM_MODEL,
                "temperature": 0.1,
                "max_output_tokens": 512,
            },
        },
        "task_order_sha256": _canonical_hash(tasks),
        "inputs_sha256": sha256_file(INPUTS),
        "packets_sha256": sha256_file(PACKETS),
        "source_raw_sha256": sha256_file(SOURCE_RAW),
        "source_report_sha256": sha256_file(SOURCE_REPORT),
        "source_reader_ab_sha256": sha256_file(SOURCE_READER_AB),
        "source_reader_summary_sha256": sha256_file(SOURCE_READER_SUMMARY),
        "sample_sha256": sha256_file(SAMPLE),
        "design_sha256": sha256_file(DESIGN),
        "qa_prompt_sha256": _canonical_hash(
            {"system": base.QA_SYSTEM, "template": base.QA_USER_TEMPLATE, "max_output_tokens": 512}
        ),
        "implementation_snapshot": _implementation_snapshot(),
        "limits": {"claims": 10, "tokens": 2_000, "concurrency": 4},
        "old_accuracy_sources": {
            "qwen_c0_c4": str(SOURCE_REPORT.resolve()),
            "glm_c4": str(SOURCE_READER_SUMMARY.resolve()),
            "glm_c0": None,
        },
    }
    write_json_atomic(PREREG, manifest)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "packets": len(packets["packets"]),
                "tasks": len(tasks),
                "preregistration": str(PREREG),
            }
        )
    )
    return 0


def _verify_snapshot(manifest: Mapping[str, Any]) -> None:
    expected_files = {
        INPUTS: str(manifest["inputs_sha256"]),
        PACKETS: str(manifest["packets_sha256"]),
        SOURCE_RAW: str(manifest["source_raw_sha256"]),
        SOURCE_REPORT: str(manifest["source_report_sha256"]),
        SOURCE_READER_AB: str(manifest["source_reader_ab_sha256"]),
        SOURCE_READER_SUMMARY: str(manifest["source_reader_summary_sha256"]),
        SAMPLE: str(manifest["sample_sha256"]),
        DESIGN: str(manifest["design_sha256"]),
    }
    if _git("rev-parse", "HEAD") != manifest.get("git_commit"):
        raise RuntimeError("git commit differs from packet RAO preregistration")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked source must stay clean during live validation")
    if manifest.get("implementation_snapshot") != _implementation_snapshot():
        raise RuntimeError("packet RAO implementation snapshot drift")
    if manifest.get("qa_prompt_sha256") != _canonical_hash(
        {"system": base.QA_SYSTEM, "template": base.QA_USER_TEMPLATE, "max_output_tokens": 512}
    ):
        raise RuntimeError("packet RAO QA prompt drift")
    for path, expected in expected_files.items():
        if not path.is_file() or sha256_file(path) != expected:
            raise RuntimeError(f"packet RAO frozen file drift: {path}")
    assert_gold_free(_json(INPUTS))
    assert_gold_free(_json(PACKETS))


def _reader_settings(settings: Any, reader_id: str) -> Any:
    if reader_id == "qwen":
        return settings
    if reader_id != "glm":
        raise ValueError(f"unsupported packet RAO reader: {reader_id}")
    key = os.environ.get(GLM_KEY_ENV)
    if not key:
        raise RuntimeError(f"{GLM_KEY_ENV} is required for glm reader")
    return dataclasses.replace(
        settings,
        llm_provider="zhipu",
        llm_base_url=GLM_BASE_URL,
        llm_model=GLM_MODEL,
        llm_api_key=key,
    )


def _completed_keys(rows: Sequence[Mapping[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (str(row["case_id"]), str(row["arm_id"]), str(row["reader_id"]))
        for row in rows
        if row.get("status") == "complete"
    }


async def _run_one(
    client: Any,
    semaphore: asyncio.Semaphore,
    task: Mapping[str, str],
    case: Mapping[str, Any],
    packet_snapshot: Mapping[str, Any],
    settings: Any,
    preregistration_sha256: str,
) -> dict[str, Any]:
    async with semaphore:
        reader_settings = _reader_settings(settings, task["reader_id"])
        packet = list(packet_snapshot["packet"])
        context = render_packet_context(packet)
        started = time.perf_counter()
        predicted, usage = await base._chat(
            client,
            reader_settings,
            system=base.QA_SYSTEM,
            user=base.QA_USER_TEMPLATE.format(packet=context or "(无)", question=case["question"]),
            max_tokens=512,
            timeout=float(reader_settings.llm_timeout),
        )
        return {
            "status": "complete",
            "preregistration_sha256": preregistration_sha256,
            "case_id": task["case_id"],
            "dataset": str(case["dataset"]),
            "category": str(case["category"]),
            "arm_id": task["arm_id"],
            "reader_id": task["reader_id"],
            "predicted_answer": predicted,
            "packet": packet,
            "top5_seed_packet": list(packet_snapshot["top5_seed_packet"]),
            "answerability": packet_snapshot["answerability"],
            "packet_tokens": sum(int(item["token_count"]) for item in packet),
            "reader_latency_seconds": time.perf_counter() - started,
            "usage": usage,
        }


async def _run_with_retry(*args: Any) -> dict[str, Any]:
    errors: list[str] = []
    for attempt in range(1, 4):
        try:
            result = await _run_one(*args)
            result["attempts"] = attempt
            result["retry_errors"] = errors
            return result
        except Exception as error:
            if not is_retryable_error(error) or attempt == 3:
                raise
            errors.append(type(error).__name__)
    raise AssertionError("unreachable")


async def command_live(concurrency: int) -> int:
    if not 1 <= concurrency <= 4:
        raise ValueError("packet RAO validation concurrency must be between 1 and 4")
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    if "coding" not in settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("qwen coding-plan endpoint/key required")
    _reader_settings(settings, "glm")
    manifest = _json(PREREG)
    _verify_snapshot(manifest)
    preregistration_sha256 = sha256_file(PREREG)
    inputs = _json(INPUTS)
    cases = {str(case["case_id"]): case for case in inputs["cases"]}
    packets = {str(item["packet_key"]): item for item in _json(PACKETS)["packets"]}
    tasks = build_tasks(inputs["cases"], preregistration_id=str(manifest["preregistration_id"]))
    existing = _read_jsonl(RAW)
    if any(row.get("preregistration_sha256") != preregistration_sha256 for row in existing):
        raise RuntimeError("existing packet RAO raw rows belong to another preregistration")
    completed = _completed_keys(existing)
    pending = [task for task in tasks if (task["case_id"], task["arm_id"], task["reader_id"]) not in completed]
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
                            task,
                            cases[task["case_id"]],
                            packets[f"{task['case_id']}|{task['arm_id']}"],
                            settings,
                            preregistration_sha256,
                        )
                        for task in batch
                    ),
                    return_exceptions=True,
                )
                for task, result in zip(batch, results, strict=True):
                    record = (
                        {
                            "status": "retryable_error" if is_retryable_error(result) else "fatal_error",
                            "preregistration_sha256": preregistration_sha256,
                            **task,
                            "error_class": type(result).__name__,
                            "error": str(result)[:500],
                        }
                        if isinstance(result, BaseException)
                        else result
                    )
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if any(isinstance(result, BaseException) and not is_retryable_error(result) for result in results):
                    raise RuntimeError("fatal packet RAO reader error recorded")
    complete = len(_completed_keys(_read_jsonl(RAW)))
    print(json.dumps({"completed": complete, "remaining": len(tasks) - complete}))
    return 0 if complete == len(tasks) else 75


def command_dry_run() -> int:
    manifest = _json(PREREG)
    _verify_snapshot(manifest)
    inputs = _json(INPUTS)
    snapshots = _json(PACKETS)["packets"]
    if len(inputs["cases"]) != 52 or len(snapshots) != 104:
        raise RuntimeError("packet RAO dry-run matrix is incomplete")
    if any(
        len(item["packet"]) > 10 or sum(int(row["token_count"]) for row in item["packet"]) > 2_000 for item in snapshots
    ):
        raise RuntimeError("packet RAO dry-run budget violation")
    tasks = build_tasks(inputs["cases"], preregistration_id=str(manifest["preregistration_id"]))
    print(json.dumps({"network_calls": 0, "cases": 52, "packets": 104, "tasks": len(tasks)}))
    return 0


def _score_answer(answer: str, packet: Sequence[Mapping[str, Any]], info: Mapping[str, Any]) -> dict[str, Any]:
    return (
        scorer.score_visible_case(answer, packet, info["gold"])
        if info["dataset"] == "relation_design_dev"
        else scorer._score_e2e(answer, packet, info)
    )


def _representation_metrics(
    inputs: Sequence[Mapping[str, Any]],
    packets: Mapping[str, Mapping[str, Any]],
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    representation: str,
) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for arm in ARMS:
        coverages: list[float] = []
        rao_matches: list[float] = []
        structured_cases = 0
        for case in inputs:
            case_id = str(case["case_id"])
            snapshot = packets[f"{case_id}|{arm}"]
            packet = snapshot["packet"] if representation == "structured" else snapshot["legacy_packet"]
            info = metadata[case_id]
            gold = scorer._gold(info["gold"])
            entity = scorer._ENTITY_SCORER.score_answer_entity_packet(packet, gold, answer_text="")
            if entity["entity_coverage_at_5"] is not None:
                coverages.append(float(entity["entity_coverage_at_5"]))
            if gold.role_action_object:
                rao_matches.append(float(scorer._rao_packet_match(packet, gold)))
            if any(item.get("role") and item.get("action") and item.get("object") for item in packet):
                structured_cases += 1
        result[arm] = {
            "entity_coverage_at_5": fmean(coverages) if coverages else 0.0,
            "packet_rao_match_rate": fmean(rao_matches) if rao_matches else 0.0,
            "packet_rao_cases": len(rao_matches),
            "structured_relation_cases": structured_cases,
            "case_count": len(inputs),
        }
    return result


def _paired(old: Mapping[str, bool], new: Mapping[str, bool]) -> dict[str, int]:
    shared = old.keys() & new.keys()
    return {
        "shared_cases": len(shared),
        "old_only_correct": sum(old[key] and not new[key] for key in shared),
        "new_only_correct": sum(new[key] and not old[key] for key in shared),
    }


def _old_results(
    metadata: Mapping[str, Mapping[str, Any]],
    packets: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, dict[str, dict[str, float | None]]],
    dict[tuple[str, str], dict[str, bool]],
]:
    old_report = _json(SOURCE_REPORT)
    old_summary = _json(SOURCE_READER_SUMMARY)
    matrix = {
        "qwen": {arm: {"accuracy": float(old_report["per_arm"][arm]["accuracy"])} for arm in ARMS},
        "glm": {
            "C0": {"accuracy": None},
            "C4": {"accuracy": float(old_summary["glm"]) / float(old_summary["n"])},
        },
    }
    qwen_runs: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for row in old_report["scored_cases"]:
        arm = str(row["arm_id"])
        if arm in ARMS:
            qwen_runs[(arm, str(row["case_id"]))].append(bool(row["score"]["answer_correct"]))
    correct: dict[tuple[str, str], dict[str, bool]] = {
        ("qwen", arm): {
            case_id: sum(values) >= 2 for (candidate_arm, case_id), values in qwen_runs.items() if candidate_arm == arm
        }
        for arm in ARMS
    }
    glm_c4: dict[str, bool] = {}
    for row in _read_jsonl(SOURCE_READER_AB):
        case_id = str(row["case_id"])
        packet = packets[f"{case_id}|C4"]["legacy_packet"]
        score = _score_answer(str(row["glm_answer"]), packet, metadata[case_id])
        glm_c4[case_id] = bool(score["answer_correct"])
    correct[("glm", "C4")] = glm_c4
    correct[("glm", "C0")] = {}
    return matrix, correct


def command_score() -> int:
    manifest = _json(PREREG)
    _verify_snapshot(manifest)
    preregistration_sha256 = sha256_file(PREREG)
    latest = {
        (str(row["case_id"]), str(row["arm_id"]), str(row["reader_id"])): row
        for row in _read_jsonl(RAW)
        if row.get("status") == "complete"
    }
    if len(latest) != 208:
        raise RuntimeError(f"packet RAO raw matrix incomplete: {len(latest)}/208")
    if any(row.get("preregistration_sha256") != preregistration_sha256 for row in latest.values()):
        raise RuntimeError("packet RAO raw preregistration binding mismatch")
    inputs = _json(INPUTS)["cases"]
    metadata = scorer._metadata(SAMPLE, DESIGN)
    packets = {str(item["packet_key"]): item for item in _json(PACKETS)["packets"]}
    scored: list[dict[str, Any]] = []
    new_correct: dict[tuple[str, str], dict[str, bool]] = defaultdict(dict)
    for (case_id, arm, reader), row in latest.items():
        score = _score_answer(str(row["predicted_answer"]), row["packet"], metadata[case_id])
        new_correct[(reader, arm)][case_id] = bool(score["answer_correct"])
        scored.append({**row, "score": score})
    old_matrix, old_correct = _old_results(metadata, packets)
    matrix: dict[str, dict[str, Any]] = {reader: {} for reader in READERS}
    for reader in READERS:
        for arm in ARMS:
            rows = [row for row in scored if row["reader_id"] == reader and row["arm_id"] == arm]
            new_accuracy = fmean(float(row["score"]["answer_correct"]) for row in rows)
            old_accuracy = old_matrix[reader][arm]["accuracy"]
            matrix[reader][arm] = {
                "old_accuracy": old_accuracy,
                "new_accuracy": new_accuracy,
                "accuracy_delta": new_accuracy - old_accuracy if old_accuracy is not None else None,
                "entity_coverage_at_5": fmean(
                    float(row["score"]["entity_coverage_at_5"])
                    for row in rows
                    if row["score"].get("entity_coverage_at_5") is not None
                ),
                "forbidden_violations": sum(bool(row["score"]["negative_violation"]) for row in rows),
                "mean_total_tokens": fmean(float(row["usage"]["total_tokens"]) for row in rows),
                "mean_packet_tokens": fmean(float(row["packet_tokens"]) for row in rows),
                "reader_latency_p95_seconds": sorted(float(row["reader_latency_seconds"]) for row in rows)[49],
                "paired": _paired(old_correct[(reader, arm)], new_correct[(reader, arm)]),
            }
    representation = {
        "legacy": _representation_metrics(inputs, packets, metadata, representation="legacy"),
        "structured": _representation_metrics(inputs, packets, metadata, representation="structured"),
    }
    output = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scorer_version": "answer-entity-packet-v1",
        "preregistration_sha256": preregistration_sha256,
        "raw_sha256": sha256_file(RAW),
        "matrix": matrix,
        "representation": representation,
        "scored_cases": scored,
        "limitations": [
            "historical glm/C0 old-render result does not exist",
            "old qwen cells use three-repeat aggregates while this directional validation uses one new pass",
        ],
    }
    write_json_atomic(REPORT, output)
    lines = [
        "# Packet RAO design/dev validation",
        "",
        "| reader | arm | old accuracy | new accuracy | delta | entity@5 | forbidden | tokens | reader p95 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for reader in READERS:
        for arm in ARMS:
            item = matrix[reader][arm]
            old = "N/A" if item["old_accuracy"] is None else f"{item['old_accuracy']:.4f}"
            delta = "N/A" if item["accuracy_delta"] is None else f"{item['accuracy_delta']:+.4f}"
            lines.append(
                f"| {reader} | {arm} | {old} | {item['new_accuracy']:.4f} | {delta} | "
                f"{item['entity_coverage_at_5']:.4f} | {item['forbidden_violations']} | "
                f"{item['mean_total_tokens']:.1f} | {item['reader_latency_p95_seconds']:.3f} |"
            )
    lines.extend(
        [
            "",
            "## Representation",
            "",
            "| arm | legacy entity@5 | new entity@5 | legacy packet RAO | new packet RAO | structured cases |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for arm in ARMS:
        old = representation["legacy"][arm]
        new = representation["structured"][arm]
        lines.append(
            f"| {arm} | {old['entity_coverage_at_5']:.4f} | {new['entity_coverage_at_5']:.4f} | "
            f"{old['packet_rao_match_rate']:.4f} | {new['packet_rao_match_rate']:.4f} | "
            f"{new['structured_relation_cases']}/{new['case_count']} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "rows": len(scored)}))
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preregister")
    sub.add_parser("dry-run")
    live = sub.add_parser("live")
    live.add_argument("--concurrency", type=int, default=4)
    sub.add_parser("score")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "preregister":
        return command_preregister()
    if args.command == "dry-run":
        return command_dry_run()
    if args.command == "live":
        return asyncio.run(command_live(args.concurrency))
    return command_score()


if __name__ == "__main__":
    raise SystemExit(main())
