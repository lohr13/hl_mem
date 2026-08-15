#!/usr/bin/env python
"""Preregister, dry-run and execute the visible C-series experiment.

The live process only reads gold-free metadata and frozen SQLite caches. Scoring
is delegated to a separate process after every raw answer has been persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hl_mem.components import initialize_process, make_embedder, make_reranker  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.evaluation.c_series import (  # noqa: E402
    ARM_IDS,
    INTENT_VERSION,
    PLANNER_OUTPUT_BUDGET,
    PLANNER_TIMEOUT_SECONDS,
    PROTOCOL_VERSION,
    arm_order,
    build_preregistration,
    case_seed,
    completed_run_keys,
    evidence_sufficiency_v1,
    extract_query_entities,
    is_retryable_error,
    parse_planner_output,
    planner_prompt,
    relation_multihop_intent_v1,
    rescue_mode,
    sha256_file,
    validate_preregistration,
    write_json_atomic,
)
from hl_mem.evaluation.c_series_runtime import (  # noqa: E402
    assert_gold_free,
    execute_planner_subgoals,
    execute_raw_rescue,
    frozen_runtime_settings,
    materialize_visible_case_cached,
    recall_visible_case,
)
from hl_mem.ingest.embedder import FakeEmbedder  # noqa: E402

SAMPLE = ROOT / "tests" / "eval" / "fixtures" / "chinese_e2e_sample.json"
DESIGN = ROOT / "tests" / "eval" / "fixtures" / "c_series_relation_design_dev.json"
INTENT_DEV = ROOT / "tests" / "eval" / "fixtures" / "c_series_intent_routing_dev.json"
INTENT_ANNOTATION_C = ROOT / "tests" / "eval" / "fixtures" / "c_series_intent_annotation_c.json"
INTENT_ANNOTATION_D = ROOT / "tests" / "eval" / "fixtures" / "c_series_intent_annotation_d.json"
CACHE_ROOT = ROOT / "var" / "eval" / "chinese_e2e_cache"
DEV_CACHE_ROOT = ROOT / "var" / "eval" / "c_series_dev_cache"
OUTPUT_ROOT = ROOT / "var" / "eval"
PREREG = OUTPUT_ROOT / "c_series_preregistration.json"
INPUTS = OUTPUT_ROOT / "c_series_inputs_nogold.json"
RAW = OUTPUT_ROOT / "c_series_raw.jsonl"
REPORT = OUTPUT_ROOT / "c_series_report.json"
REPORT_MD = OUTPUT_ROOT / "c_series_report.md"
QA_SYSTEM = (
    "你是记忆问答助手。只能根据给定记忆回答；证据不足时只回答“信息不足”。"
    "区分推荐与执行、报道与拥有；涉及全部时完整列举。答案简洁，不解释。"
)
QA_USER_TEMPLATE = "记忆片段:\n{packet}\n\n问题: {question}"
PLANNER_SYSTEM = (
    "你是关系检索规划器，不回答问题。仅输出JSON对象，键subgoals；最多两个子目标，"
    "每项仅含query和max_depth，深度只能为1或2。"
)
PLANNER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subgoals"],
    "properties": {
        "subgoals": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query", "max_depth"],
                "properties": {"query": {"type": "string"}, "max_depth": {"enum": [1, 2]}},
            },
        }
    },
}
EXPECTED_SEALED_SHA256 = "1e4be5bbc93cfefd31d1d78a0c7b96cddccbe5ac8a3f71f90e77f8471b24f0a1"
RUNNER_IMPLEMENTATION_VERSION = "c-series-runner-v3"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _safe_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", case_id).strip("._-")[:72] or "case"


def _prompt_hashes() -> dict[str, str]:
    extractor = importlib.import_module("hl_mem.ingest.llm_extractor")
    schemas = importlib.import_module("hl_mem.ingest.schemas")
    extraction = {
        "system_prompt": extractor.SYSTEM_PROMPT,
        "english_system_prompt": extractor.ENGLISH_SYSTEM_PROMPT,
        "schema": schemas.extraction_response_json_schema(),
        "prompt_hash": extractor.PROMPT_HASH,
    }
    return {
        "extraction_prompt_schema": _canonical_hash(extraction),
        "qa_system_user_template": _canonical_hash(
            {"system": QA_SYSTEM, "user_template": QA_USER_TEMPLATE, "max_output": 512}
        ),
        "planner_system_user_schema": _canonical_hash(
            {
                "system": PLANNER_SYSTEM,
                "user_builder_source": inspect.getsource(planner_prompt),
                "schema": PLANNER_SCHEMA,
                "max_output": PLANNER_OUTPUT_BUDGET,
                "timeout": PLANNER_TIMEOUT_SECONDS,
            }
        ),
        "intent_rules": _canonical_hash(
            {"version": INTENT_VERSION, "source": sha256_file(ROOT / "src" / "hl_mem" / "evaluation" / "c_series.py")}
        ),
        "query_entity_implementation": _canonical_hash(
            {
                "function": extract_query_entities.__name__,
                "source": sha256_file(ROOT / "src" / "hl_mem" / "evaluation" / "c_series.py"),
            }
        ),
        "tokenizer": _canonical_hash(
            {
                "lexicalizer": sha256_file(ROOT / "src" / "hl_mem" / "recall" / "lexicalizer.py"),
                "budget": "unicode-char-ceil-div-2-v1",
            }
        ),
    }


def _runtime_fingerprint(settings: Any) -> str:
    runtime = frozen_runtime_settings(settings)
    fields = {
        "vector_backend": str(runtime.vector_backend),
        "vector_scan_limit": runtime.recall_vector_scan_limit,
        "candidate_floor": runtime.recall_candidate_floor,
        "query_expansion_mode": runtime.query_expansion_mode,
        "tag_channel_enabled": runtime.tag_channel_enabled,
        "relation_discovery_mode": runtime.relation_discovery_mode,
        "relevance_gate_mode": runtime.relevance_gate_mode,
        "packed_context_token_budget": runtime.packed_context_token_budget,
        "embedding_model": runtime.embedding_model,
        "embedding_dim": runtime.embedding_dim,
        "reranker_model": runtime.reranker_model,
        "reader_model": runtime.llm_model,
    }
    return _canonical_hash(fields)


def _implementation_snapshot() -> dict[str, str]:
    files = {
        "protocol": ROOT / "src" / "hl_mem" / "evaluation" / "c_series.py",
        "runtime": ROOT / "src" / "hl_mem" / "evaluation" / "c_series_runtime.py",
        "runner": Path(__file__),
        "scorer": ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py",
    }
    return {
        "version": RUNNER_IMPLEMENTATION_VERSION,
        **{f"{name}_sha256": sha256_file(path) for name, path in files.items()},
    }


def _wilson(successes: int, total: int) -> dict[str, float]:
    if total <= 0:
        return {"low": 0.0, "high": 0.0}
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return {"low": center - radius, "high": center + radius}


def _expand_intent_annotation(path: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    artifact = _json(path)
    if artifact.get("annotation_kind") != "independent-agent-blind" or not artifact.get("annotator"):
        raise RuntimeError(f"intent annotation is not independent-agent provenance: {path}")
    labels: dict[str, bool] = {}
    if (
        artifact.get("annotator_visible_fields") != ["blind_id", "query"]
        or artifact.get("human_annotation") is not False
    ):
        raise RuntimeError(f"intent annotation did not preserve blind non-human provenance: {path}")
    for rule in artifact.get("adjudicated_original_id_label_runs") or []:
        for index in range(int(rule["start"]), int(rule["end_inclusive"]) + 1):
            case_id = f"{rule['id_prefix']}{index:03d}"
            if case_id in labels:
                raise RuntimeError(f"duplicate intent annotation ID: {case_id}")
            labels[case_id] = bool(rule["label"])
    blind_labels = {
        hashlib.sha256(f"c-series-intent-blind-v1|{case_id}".encode("utf-8")).hexdigest()[:20]: label
        for case_id, label in labels.items()
    }
    blind_sha = _canonical_hash(blind_labels)
    if blind_sha != artifact.get("blind_labels_sha256"):
        raise RuntimeError(f"blind intent annotation digest mismatch: {path}")
    if sum(labels.values()) != int(artifact.get("true_count", -1)) or len(labels) - sum(labels.values()) != int(
        artifact.get("false_count", -1)
    ):
        raise RuntimeError(f"blind intent annotation counts mismatch: {path}")
    return artifact, labels


def _intent_metrics() -> dict[str, Any]:
    fixture = _json(INTENT_DEV)
    cases = fixture["cases"]
    if len(cases) < 200 or len({item["query"] for item in cases}) != len(cases):
        raise RuntimeError("intent dev must contain at least 200 unique queries")
    adjudicated = {str(item["id"]): bool(item["needs_relation_or_multihop"]) for item in cases}
    artifact_c, labels_c = _expand_intent_annotation(INTENT_ANNOTATION_C)
    artifact_d, labels_d = _expand_intent_annotation(INTENT_ANNOTATION_D)
    if artifact_c["annotator"] == artifact_d["annotator"]:
        raise RuntimeError("intent annotations must have independent annotator IDs")
    if labels_c.keys() != adjudicated.keys() or labels_d.keys() != adjudicated.keys():
        raise RuntimeError("intent annotations must cover exactly the adjudicated IDs")
    disagreements = [case_id for case_id in adjudicated if labels_c[case_id] != labels_d[case_id]]
    adjudication_mismatches = [
        case_id for case_id, label in adjudicated.items() if labels_c[case_id] != label or labels_d[case_id] != label
    ]
    if disagreements or adjudication_mismatches:
        raise RuntimeError("intent annotation disagreement requires explicit adjudication")
    predictions = [relation_multihop_intent_v1(str(item["query"])).eligible for item in cases]
    tp = sum(pred and item["needs_relation_or_multihop"] for pred, item in zip(predictions, cases, strict=True))
    fp = sum(pred and not item["needs_relation_or_multihop"] for pred, item in zip(predictions, cases, strict=True))
    fn = sum(not pred and item["needs_relation_or_multihop"] for pred, item in zip(predictions, cases, strict=True))
    tn = len(cases) - tp - fp - fn
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    fpr = fp / (fp + tn)
    f1_positive = 2 * precision * recall / (precision + recall)
    negative_precision = tn / (tn + fn)
    negative_recall = tn / (tn + fp)
    f1_negative = 2 * negative_precision * negative_recall / (negative_precision + negative_recall)
    category_recall: dict[str, float] = {}
    for category in sorted({str(item["category"]) for item in cases if item["needs_relation_or_multihop"]}):
        pairs = [
            pred
            for pred, item in zip(predictions, cases, strict=True)
            if item["needs_relation_or_multihop"] and item["category"] == category
        ]
        category_recall[category] = sum(pairs) / len(pairs)
    result = {
        "cases": len(cases),
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": precision,
        "recall": recall,
        "fpr": fpr,
        "macro_f1": (f1_positive + f1_negative) / 2,
        "category_recall": category_recall,
        "wilson_95": {
            "precision": _wilson(tp, tp + fp),
            "recall": _wilson(tp, tp + fn),
            "fpr": _wilson(fp, fp + tn),
        },
        "independent_annotations": {
            "artifacts": [
                {
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                    "raw_source_sha256": artifact["raw_source_sha256"],
                    "annotator": artifact["annotator"],
                    "model": artifact["model"],
                }
                for path, artifact in (
                    (INTENT_ANNOTATION_C, artifact_c),
                    (INTENT_ANNOTATION_D, artifact_d),
                )
            ],
            "agreement": 1.0,
            "disagreement_count": len(disagreements),
            "adjudication_required": len(disagreements),
            "adjudication_mismatch_count": len(adjudication_mismatches),
            "blind_id_rule": "sha256('c-series-intent-blind-v1|'+original_id)[:20]",
            "provenance": "blind independent-agent; non-human annotation",
        },
    }
    precision_wilson = result["wilson_95"]["precision"]
    recall_wilson = result["wilson_95"]["recall"]
    fpr_wilson = result["wilson_95"]["fpr"]
    result["gate_passed"] = (
        precision >= 0.90
        and recall >= 0.90
        and fpr <= 0.05
        and precision_wilson["low"] >= 0.82
        and recall_wilson["low"] >= 0.82
        and fpr_wilson["high"] <= 0.10
        and len(category_recall) >= 6
        and all(value >= 0.80 for value in category_recall.values())
    )
    if not result["gate_passed"]:
        raise RuntimeError(f"intent router preregistration gate failed: {result}")
    return result


def _case_source_corpora(dataset: str) -> list[dict[str, str]]:
    sources = _json(SAMPLE)["sources"]
    if dataset == "perltqa":
        names = ("perltqa_memory", "perltqa_qa")
        return [
            {"id": f"source_{name}", "sha256": sha256_file(Path(str(sources[name]["path"])).resolve())}
            for name in names
        ]
    if dataset == "memdaily":
        source = sources["memdaily"]
        return [
            {
                "id": "source_memdaily",
                "sha256": sha256_file(Path(str(source["path"])).resolve()),
            }
        ]
    return [{"id": "visible_relation_dev", "sha256": sha256_file(DESIGN)}]


def _case_runtime_contract(case: Mapping[str, Any], db_path: Path, dataset: str) -> dict[str, Any]:
    return {
        **case,
        "allowed_modalities": ["text"],
        "source_cache_identity": str(db_path.resolve()),
        "source_cache_sha256": sha256_file(db_path),
        "source_corpora": _case_source_corpora(dataset),
    }


def _materialize_dev(settings: Any) -> list[dict[str, Any]]:
    DEV_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    result: list[dict[str, Any]] = []
    for raw in _json(DESIGN)["cases"]:
        case = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "answer",
                "gold",
            }
        }
        db_path = DEV_CACHE_ROOT / f"{_safe_name(str(case['case_id']))}.db"
        resolved = db_path.resolve()
        if not resolved.is_relative_to(DEV_CACHE_ROOT.resolve()):
            raise RuntimeError("dev cache path escaped its root")
        materialize_visible_case_cached(db_path, case, settings)
        result.append(
            _case_runtime_contract(
                {
                    "case_id": case["case_id"],
                    "dataset": "relation_design_dev",
                    "category": case["category"],
                    "question": case["question"],
                    "question_at": case["question_at"],
                    "known_as_of": case.get("known_as_of"),
                    "namespace": case["namespace"],
                    "db_path": str(db_path.resolve()),
                },
                db_path,
                "relation_design_dev",
            )
        )
    return result


def _e2e_inputs() -> list[dict[str, Any]]:
    chinese = importlib.import_module("tests.eval.chinese_e2e")
    memdaily = importlib.import_module("evaluation.tools.run_memdaily_benchmark")
    manifest = chinese.load_sample_manifest(SAMPLE)
    sampled = chinese.load_sampled_inputs(manifest)
    result: list[dict[str, Any]] = []
    for bundle in sampled.perltqa_bundles:
        ingest = chinese.build_perltqa_ingest_trajectory(bundle)
        db_path, _ = chinese._cache_paths(CACHE_ROOT, "perltqa", ingest.case_id)
        for question in bundle.questions:
            trajectory = chinese.build_perltqa_question_trajectory(ingest, question)
            result.append(
                _case_runtime_contract(
                    {
                        "case_id": question.case_id,
                        "dataset": "chinese_e2e",
                        "category": f"perltqa_{question.category}",
                        "question": question.question,
                        "question_at": trajectory.question_at,
                        "known_as_of": None,
                        "namespace": question.namespace,
                        "db_path": str(db_path.resolve()),
                    },
                    db_path,
                    "perltqa",
                )
            )
    for trajectory in sampled.memdaily_trajectories:
        db_path = CACHE_ROOT / "memdaily" / f"{memdaily._safe_case_name(trajectory.case_id)}.db"
        result.append(
            _case_runtime_contract(
                {
                    "case_id": trajectory.case_id,
                    "dataset": "chinese_e2e",
                    "category": f"memdaily_{trajectory.qtype}",
                    "question": trajectory.question,
                    "question_at": trajectory.question_at,
                    "known_as_of": None,
                    "namespace": trajectory.namespace,
                    "db_path": str(db_path.resolve()),
                },
                db_path,
                "memdaily",
            )
        )
    if len(result) != 40 or any(not Path(item["db_path"]).is_file() for item in result):
        raise RuntimeError("frozen 40-case cache mapping is incomplete")
    return result


def _source_corpora() -> dict[str, Path]:
    sources = _json(SAMPLE)["sources"]
    result = {
        "chinese_e2e_manifest": SAMPLE,
        "visible_relation_dev": DESIGN,
        "intent_routing_dev": INTENT_DEV,
        "intent_annotation_c": INTENT_ANNOTATION_C,
        "intent_annotation_d": INTENT_ANNOTATION_D,
    }
    for name, source in sources.items():
        result[f"source_{name}"] = Path(str(source["path"])).resolve()
    return result


def _snapshot_files(corpora: Mapping[str, Path], caches: Sequence[Path]) -> dict[str, str]:
    paths = [*corpora.values(), *caches, ROOT / "hl_mem.toml", ROOT / "uv.lock"]
    return {str(path.resolve()): sha256_file(path.resolve()) for path in sorted(set(paths))}


def command_preregister() -> int:
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    if "coding" not in settings.llm_base_url:
        raise RuntimeError("reader/planner must use the configured coding-plan endpoint")
    commit = _git("rev-parse", "HEAD")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("source snapshot must be clean and committed")
    cases = [*_e2e_inputs(), *_materialize_dev(settings)]
    inputs = {
        "schema_version": 2,
        "protocol_version": PROTOCOL_VERSION,
        "cases": cases,
    }
    assert_gold_free(inputs)
    write_json_atomic(INPUTS, inputs)
    caches = sorted({Path(item["db_path"]) for item in cases})
    cache_manifests = sorted(CACHE_ROOT.rglob("*.manifest.json"))
    dev_cache_manifests = sorted(DEV_CACHE_ROOT.rglob("*.manifest.json"))
    all_caches = [*caches, *cache_manifests, *dev_cache_manifests]
    corpora = _source_corpora()
    prompt_hashes = _prompt_hashes()
    manifest = build_preregistration(
        preregistration_id=f"c-series-design-dev-runner-v3-{commit[:12]}",
        git_commit=commit,
        clean_source=True,
        corpus_paths={**corpora, "gold_free_inputs": INPUTS},
        cache_paths=all_caches,
        model_snapshot={
            "extractor": {
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "revision": sorted({str(_json(path).get("extractor_version") or "") for path in cache_manifests}),
            },
            "embedder": {
                "provider": "dashscope",
                "model": settings.embedding_model,
                "revision": settings.embedding_model,
            },
            "reranker": {
                "provider": settings.reranker_provider,
                "model": settings.reranker_model,
                "revision": settings.reranker_model,
            },
            "reader": {
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "revision": settings.llm_model,
                "endpoint": settings.llm_base_url,
                "endpoint_class": "coding-plan",
                "temperature": 0.1,
                "seed_support": "unsupported",
            },
            "planner": {
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "revision": settings.llm_model,
                "endpoint": settings.llm_base_url,
                "endpoint_class": "coding-plan",
                "temperature": 0.1,
                "timeout_seconds": PLANNER_TIMEOUT_SECONDS,
            },
        },
        prompt_hashes=prompt_hashes,
        case_ids=[str(item["case_id"]) for item in cases],
    )
    manifest.update(
        {
            "inputs_sha256": sha256_file(INPUTS),
            "snapshot_files": _snapshot_files({**corpora, "gold_free_inputs": INPUTS}, all_caches),
            "runtime_config_sha256": _runtime_fingerprint(settings),
            "implementation_snapshot": _implementation_snapshot(),
            "hl_mem_toml_sha256": sha256_file(ROOT / "hl_mem.toml"),
            "intent_dev": _intent_metrics(),
            "extraction_cache_config": {
                str(path.resolve()): {
                    key: value
                    for key, value in _json(path).items()
                    if key
                    in {
                        "schema_version",
                        "case_id",
                        "case_fingerprint",
                        "ingest_config_fingerprint",
                        "extractor_model",
                        "extractor_version",
                        "embedding_model",
                        "embedding_dim",
                        "embedding_api_mode",
                        "embedding_text_type",
                        "index_text_mode",
                        "index_text_version",
                    }
                }
                for path in cache_manifests
            },
            "category_distribution": dict(sorted(Counter(item["category"] for item in cases).items())),
            "sealed_payload_sha256": EXPECTED_SEALED_SHA256,
        }
    )
    validate_preregistration(manifest)
    write_json_atomic(PREREG, manifest)
    print(json.dumps({"cases": len(cases), "cache_files": len(all_caches), "preregistration": str(PREREG)}))
    return 0


def _verify_live_snapshot(manifest: Mapping[str, Any], settings: Any) -> None:
    validate_preregistration(manifest)
    if _git("rev-parse", "HEAD") != manifest["git_commit"]:
        raise RuntimeError("git commit differs from preregistration")
    if _git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("live requires a clean source snapshot")
    if manifest["prompt_hashes"] != _prompt_hashes():
        raise RuntimeError("prompt/schema hash drift")
    if manifest["runtime_config_sha256"] != _runtime_fingerprint(settings):
        raise RuntimeError("runtime experiment config drift")
    if manifest.get("implementation_snapshot") != _implementation_snapshot():
        raise RuntimeError("runner implementation snapshot drift")
    for raw_path, expected in manifest["snapshot_files"].items():
        path = Path(raw_path)
        if not path.is_file() or sha256_file(path) != expected:
            raise RuntimeError(f"snapshot file drift: {path}")
    inputs = _json(INPUTS)
    assert_gold_free(inputs)
    if sha256_file(INPUTS) != manifest["inputs_sha256"]:
        raise RuntimeError("gold-free input snapshot drift")
    if not manifest["intent_dev"]["gate_passed"]:
        raise RuntimeError("intent router preregistration gate failed")


def _tasks(manifest: Mapping[str, Any], inputs: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], int, str]]:
    corpus_sha = _canonical_hash(manifest["corpora"])
    tasks: list[tuple[Mapping[str, Any], int, str]] = []
    for case in inputs["cases"]:
        for repeat in range(3):
            seed = case_seed(manifest["preregistration_id"], corpus_sha, str(case["case_id"]), repeat)
            tasks.extend((case, repeat, arm) for arm in arm_order(seed))
    return tasks


def command_dry_run() -> int:
    manifest = _json(PREREG)
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    _verify_live_snapshot(manifest, settings)
    inputs = _json(INPUTS)
    dry_settings = dataclasses.replace(settings, recall_dense_enabled=False, reranker_mode="off")
    for case in inputs["cases"]:
        for arm in ARM_IDS:
            result = recall_visible_case(
                case,
                dry_settings,
                FakeEmbedder(settings.embedding_dim),
                None,
                db_path=Path(case["db_path"]),
                arm_id=arm,
            )
            if len(result.packet) > 10 or sum(int(item["token_count"]) for item in result.packet) > 2_000:
                raise RuntimeError(f"dry-run packet budget failed: {case['case_id']} {arm}")
    print(
        json.dumps(
            {
                "network_calls": 0,
                "cases": len(inputs["cases"]),
                "arm_recall_smoke_count": len(inputs["cases"]) * len(ARM_IDS),
                "tasks": len(_tasks(manifest, inputs)),
            }
        )
    )
    return 0


async def _chat(
    client: Any,
    settings: Any,
    *,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> tuple[str, dict[str, int]]:
    assert_gold_free({"system_prompt": system, "user_prompt": user})
    response = await client.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
        json={
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    content = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    usage = payload.get("usage") or {}
    return content, {
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _sufficiency(case: Mapping[str, Any], execution: Any) -> Any:
    intent = relation_multihop_intent_v1(str(case["question"]))
    covered = {
        component
        for component in intent.required_rao
        if any(item.get(component) and item.get("evidence_event_ids") for item in execution.packet)
    }
    entities = [
        entity for item in execution.packet if item.get("evidence_event_ids") for entity in item.get("entities") or []
    ]
    return intent, evidence_sufficiency_v1(
        answerability=execution.answerability,
        required_rao=intent.required_rao,
        covered_rao=tuple(covered),
        query_entities=extract_query_entities(str(case["question"])),
        packet_entities=entities,
    )


async def _run_one(
    client: Any,
    semaphore: asyncio.Semaphore,
    case: Mapping[str, Any],
    repeat: int,
    arm: str,
    settings: Any,
    embedder: Any,
    reranker: Any,
    *,
    retry_attempt: int = 1,
    planner_fallback_on_retryable: bool = False,
) -> dict[str, Any]:
    async with semaphore:
        overall = time.perf_counter()
        execution = await asyncio.to_thread(
            recall_visible_case,
            case,
            settings,
            embedder,
            reranker,
            db_path=Path(case["db_path"]),
            arm_id=arm,
        )
        intent, sufficiency = _sufficiency(case, execution)
        rescue = rescue_mode(arm, intent=intent.eligible, insufficient=sufficiency.insufficient)
        packet = execution.packet
        planner_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        planner_error = None
        planner_attempts = 0
        if rescue == "raw":
            packet = await asyncio.to_thread(
                execute_raw_rescue,
                Path(case["db_path"]),
                case,
                execution,
                query=str(case["question"]),
                settings=settings,
            )
        elif rescue == "planner":
            planner_attempts = retry_attempt
            try:
                visible_edges = [
                    hop
                    for candidate in execution.search_trace.get("candidates", {}).values()
                    if isinstance(candidate, Mapping)
                    for path in candidate.get("relation_paths") or []
                    if isinstance(path, Mapping)
                    for hop in path.get("path") or []
                    if isinstance(hop, Mapping)
                ]
                prompt = planner_prompt(str(case["question"]), execution.seed_packet, visible_edges)
                planned, planner_usage = await _chat(
                    client,
                    settings,
                    system=PLANNER_SYSTEM,
                    user=prompt,
                    max_tokens=PLANNER_OUTPUT_BUDGET,
                    timeout=PLANNER_TIMEOUT_SECONDS,
                )
                subgoals = parse_planner_output(planned)
                packet = await asyncio.to_thread(
                    execute_planner_subgoals,
                    Path(case["db_path"]),
                    case,
                    execution,
                    subgoals,
                    settings=settings,
                    embedder=embedder,
                    reranker=reranker,
                )
            except Exception as error:
                if is_retryable_error(error) and not planner_fallback_on_retryable:
                    raise
                planner_error = type(error).__name__
                packet = execution.packet
        context = "\n".join(f"[{index}] {item['text']}" for index, item in enumerate(packet, start=1))
        predicted, usage = await _chat(
            client,
            settings,
            system=QA_SYSTEM,
            user=QA_USER_TEMPLATE.format(packet=context or "(无)", question=case["question"]),
            max_tokens=512,
            timeout=float(settings.llm_timeout),
        )
        return {
            "status": "complete",
            "case_id": case["case_id"],
            "dataset": case["dataset"],
            "category": case["category"],
            "repeat_index": repeat,
            "arm_id": arm,
            "predicted_answer": predicted,
            "packet": list(packet),
            "top5_seed_packet": list(execution.seed_packet),
            "answerability": execution.answerability,
            "intent": dataclasses.asdict(intent),
            "sufficiency": dataclasses.asdict(sufficiency),
            "rescue": rescue,
            "search_trace": execution.search_trace,
            "recall_latency_seconds": execution.recall_latency_seconds,
            "e2e_latency_seconds": time.perf_counter() - overall,
            "usage": usage,
            "planner_usage": planner_usage,
            "planner_error": planner_error,
            "planner_attempts": planner_attempts,
            "reader_seed": None,
            "seed_support": "unsupported",
        }


async def _run_with_retry(*args: Any) -> dict[str, Any]:
    task_started_at = time.perf_counter()
    errors: list[str] = []
    for attempt in range(1, 4):
        try:
            result = await _run_one(
                *args,
                retry_attempt=attempt,
                planner_fallback_on_retryable=attempt == 3,
            )
            result["e2e_latency_seconds"] = time.perf_counter() - task_started_at
            result["attempts"] = attempt
            result["retry_errors"] = errors
            return result
        except Exception as error:
            if not is_retryable_error(error) or attempt == 3:
                raise
            errors.append(type(error).__name__)
    raise AssertionError("unreachable")


async def command_live(concurrency: int) -> int:
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    settings = load_settings(ROOT / "hl_mem.toml", ROOT / ".env")
    manifest = _json(PREREG)
    _verify_live_snapshot(manifest, settings)  # must happen before constructing network clients
    if "coding" not in settings.llm_base_url or not settings.llm_api_key:
        raise RuntimeError("coding-plan endpoint/key required")
    initialize_process(settings)
    embedder = make_embedder(settings)
    reranker = make_reranker(settings)
    inputs = _json(INPUTS)
    completed = completed_run_keys(RAW)
    pending = [
        task for task in _tasks(manifest, inputs) if (str(task[0]["case_id"]), task[1], task[2]) not in completed
    ]
    import httpx

    semaphore = asyncio.Semaphore(concurrency)
    with RAW.open("a", encoding="utf-8") as handle:
        async with httpx.AsyncClient() as client:
            for offset in range(0, len(pending), concurrency):
                batch = pending[offset : offset + concurrency]
                results = await asyncio.gather(
                    *(
                        _run_with_retry(client, semaphore, case, repeat, arm, settings, embedder, reranker)
                        for case, repeat, arm in batch
                    ),
                    return_exceptions=True,
                )
                for task, result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException):
                        case, repeat, arm = task
                        record = {
                            "status": "retryable_error" if is_retryable_error(result) else "fatal_error",
                            "case_id": case["case_id"],
                            "repeat_index": repeat,
                            "arm_id": arm,
                            "error_class": type(result).__name__,
                            "error": str(result)[:500],
                        }
                    else:
                        record = result
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if any(isinstance(item, BaseException) and not is_retryable_error(item) for item in results):
                    raise RuntimeError("fatal live error recorded")
    remaining = len(_tasks(manifest, inputs)) - len(completed_run_keys(RAW))
    print(json.dumps({"completed": len(completed_run_keys(RAW)), "remaining": remaining}))
    return 0 if remaining == 0 else 75


def command_score() -> int:
    scorer = ROOT / "evaluation" / "tools" / "score_c_series_relation_experiment.py"
    result = subprocess.run(
        [
            sys.executable,
            str(scorer),
            "--raw",
            str(RAW),
            "--inputs",
            str(INPUTS),
            "--sample",
            str(SAMPLE),
            "--design",
            str(DESIGN),
            "--prereg",
            str(PREREG),
            "--output",
            str(REPORT),
            "--markdown",
            str(REPORT_MD),
        ],
        cwd=ROOT,
        check=False,
    )
    return result.returncode


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preregister")
    sub.add_parser("dry-run")
    live = sub.add_parser("live")
    live.add_argument("--concurrency", type=int, default=8)
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
