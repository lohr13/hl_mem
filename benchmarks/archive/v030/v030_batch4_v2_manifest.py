"""Deterministic E2/E3/E4 v2 corpus projections."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.archive.v030.v030_corpus import SCHEMA_VERSION, manifest_sha256, validate_manifest, write_manifest
from hl_mem.domain.action_coordinates import project_action_qualifiers
from hl_mem.domain.claims.dedup import dedup_structural_gate
from hl_mem.domain.entity import typed_builtin_seeds
from hl_mem.domain.entity_coordinates import normalize_typed_alias

_E3_TEXT = {
    "correction": "The user corrected the earlier setting: always use port 8090.",
    "guardrail": "To prevent data loss, always verify the backup before every migration.",
    "high_cost": "A failed migration deleted production records; never run it without a backup.",
    "persistent_instruction": "From now on, always validate the database backup before deployment.",
    "bait_negative": "The postmortem used the word lesson, but the blue theme remains an ordinary preference.",
    "ordinary": "The dashboard theme is blue.",
}


def _builtin_aliases() -> dict[str, str]:
    return {normalize_typed_alias(item.alias): item.canonical_entity_id for item in typed_builtin_seeds().aliases}


def derive_e2_case(case: Mapping[str, Any], pair_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Attach only source-proved coordinates; never invent an unknown entity type."""

    result = copy.deepcopy(dict(case))
    claims = result["input"]["claims"]
    aliases = _builtin_aliases()
    subjects = [normalize_typed_alias(str(claim.get("subject_entity_id") or "")) for claim in claims]
    canonical = [aliases.get(subject) for subject in subjects]
    for claim, entity_id in zip(claims, canonical, strict=True):
        claim["status"] = "active"
        claim["subject_canonical_entity_id"] = entity_id
        is_plan = str(claim.get("assertion_kind") or "") == "plan" or str(
            claim.get("canonical_attribute") or ""
        ).startswith("plan.")
        claim["qualifiers"] = project_action_qualifiers(
            str(claim.get("value") or ""), claim.get("qualifiers"), is_plan=is_plan
        )
        claim["entity_proof_id"] = hashlib.sha256(
            f"e2-v2:{claim['id']}:{entity_id or subjects[0]}".encode()
        ).hexdigest()
    proof_kind = "typed_alias" if all(canonical) else "legacy_same_subject"
    if len(set(subjects)) != 1 and not all(canonical):
        proof_kind = "unresolved"
    result["input"]["coordinate_proof"] = {
        "kind": proof_kind,
        "canonical_entity_ids": canonical,
        "subject_token_sha256": hashlib.sha256(subjects[0].encode()).hexdigest(),
    }
    result["input"]["judge_confidence"] = pair_metadata.get("judge_confidence")
    gate = dedup_structural_gate(claims[0], claims[1], allow_cross_subject=subjects[0] != subjects[1])
    result["input"]["hard_validator"] = {"safe": gate.safe, "reason": gate.reason}
    return result


def derive_e3_case(case: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(case))
    suffix = str(result["case_id"]).rsplit(":", 1)[-1]
    index = int(suffix) if suffix.isdigit() else 0
    base = _E3_TEXT[str(result["category"])]
    result["input"]["text"] = f"{base} Reference case {index:03d}."
    return result


def derive_e4_case(
    category: str,
    index: int,
    entity_id: str | None,
    relevant_claim_ids: Sequence[str],
) -> dict[str, Any]:
    entities = [entity_id] if entity_id else []
    return {
        "case_id": f"e4-v2:{category}:{index:03d}",
        "category": category,
        "source": "snapshot_derived_synthetic_query",
        "input": {
            "query": f"entity probe {category} {index:03d}",
            "query_id_hash": hashlib.sha256(f"e4-v2:{category}:{index:03d}".encode()).hexdigest(),
        },
        "gold": {
            "decision": "resolve" if entity_id else "wide",
            "entity_ids": entities,
            "relevant_claim_ids": list(relevant_claim_ids),
            "gold_status": "deterministic_snapshot_v2",
        },
    }


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(source_id: str, path: str | Path) -> dict[str, Any]:
    return {"source_id": source_id, "sha256": _sha256(path), "reconstructable": True, "path_hint": Path(path).name}


def _seal(
    experiment: str,
    cases: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    source_audit: Mapping[str, Any],
    preregistration: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "design_baseline": "2026-08-25",
        "corpus_revision": "v2",
        "experiment": experiment,
        "source_snapshots": sorted((dict(item) for item in snapshots), key=lambda item: item["source_id"]),
        "source_audit": copy.deepcopy(dict(source_audit)),
        "preregistration": copy.deepcopy(dict(preregistration)),
        "cases": sorted((copy.deepcopy(dict(case)) for case in cases), key=lambda item: item["case_id"]),
    }
    validate_manifest(manifest)
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_manifest(manifest)
    return manifest


def _pair_metadata(database_path: str | Path, volcano_path: str | Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{Path(database_path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id,decision,judge_confidence,judge_model,judge_reason,reviewed_at,similarity FROM dedup_pairs"
        ).fetchall()
    finally:
        connection.close()
    result = {str(row["id"]): dict(row) for row in rows}
    volcano = json.loads(Path(volcano_path).read_text(encoding="utf-8"))
    result.update({str(row["id"]): dict(row) for row in volcano["pairs"]})
    return result


def build_e2_v2_preregistration(
    v1_path: str | Path,
    database_path: str | Path,
    volcano_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    v1 = json.loads(Path(v1_path).read_text(encoding="utf-8"))
    metadata = _pair_metadata(database_path, volcano_path)
    cases: list[dict[str, Any]] = []
    for raw in v1["cases"]:
        pair_id = str(raw["input"]["pair_id"])
        if pair_id not in metadata:
            raise ValueError(f"E2 pair metadata missing: {pair_id}")
        case = derive_e2_case(raw, metadata[pair_id])
        case["input"]["historical_decision"] = case["gold"]["decision"]
        case["label_provenance"] = {
            "historical_pair_id": pair_id,
            "hard_validator": case["input"]["hard_validator"],
            "blind_judgment": "pending_qwen_double_permutation",
        }
        case["gold"]["gold_status"] = "pending_blind_v2"
        cases.append(case)
    typed = sum(case["input"]["coordinate_proof"]["kind"] == "typed_alias" for case in cases)
    manifest = _seal(
        "E2",
        cases,
        [
            _snapshot("e2_v1_manifest", v1_path),
            _snapshot("local_snapshot", database_path),
            _snapshot("volcano_e2_evidence", volcano_path),
        ],
        {
            "real_cases": len(cases),
            "synthetic_cases": 0,
            "typed_alias_pairs": typed,
            "legacy_same_subject_pairs": len(cases) - typed,
            "derivation": "offline_batch2_entity_and_batch3_action_coordinates",
        },
        {
            "arms": {"A": "audit_only", "B": 0.99, "C": 0.98},
            "minimum_cases": 406,
            "minimum_eligible_per_floor": 100,
            "auto_precision": 1.0,
            "wilson_lower_min": 0.96,
            "protected_violations": 0,
            "rollback_reversible": 1.0,
            "recall_absolute_drop_max": 0.01,
            "recall_regression_p_min": 0.05,
            "qwen_batch_size": 10,
            "double_permutation": True,
        },
    )
    write_manifest(output_path, manifest)
    return manifest


def build_e3_v2_manifest(v1_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    v1 = json.loads(Path(v1_path).read_text(encoding="utf-8"))
    cases = [derive_e3_case(case) for case in v1["cases"]]
    for case in cases:
        suffix = int(str(case["case_id"]).rsplit(":", 1)[-1])
        bounded = (
            case["category"] in {"correction", "guardrail", "high_cost", "persistent_instruction"} and suffix % 10 == 0
        )
        case["input"]["time_bounded"] = bounded
        if bounded:
            case["input"]["text"] += " This instruction applies only until Friday."
        case["gold"]["retention"] = (
            "temporal" if bounded else ("permanent" if case["gold"]["decision"] == "high" else "default")
        )
    manifest = _seal(
        "E3",
        cases,
        [_snapshot("e3_v1_manifest", v1_path)],
        {
            "real_cases": 0,
            "synthetic_cases": len(cases),
            "rule_frozen_gold": len(cases),
            "time_bounded_cases": sum(bool(case["input"]["time_bounded"]) for case in cases),
            "derivation": "deterministic_rule_templates_v2",
        },
        {
            "arms": {"A": "old_prompt", "B": "lesson_prompt_v1"},
            "target_signal_recall_min": 0.90,
            "high_precision_min": 0.95,
            "bait_high_false_positive_max": 0.05,
            "general_extraction_absolute_drop_max": 0.01,
            "time_bounded_permanent_errors": 0,
            "qwen_batch_size": 10,
            "double_permutation": True,
        },
    )
    write_manifest(output_path, manifest)
    return manifest


def _decode(value: Any) -> str:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return str(value or "")
    return decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False, sort_keys=True)


def _e4_claim_groups(database_path: str | Path) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    aliases = _builtin_aliases()
    connection = sqlite3.connect(f"file:{Path(database_path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id,subject_entity_id,value_json,index_text FROM claims "
            "WHERE status IN ('active','candidate','disputed') ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    unresolved: list[dict[str, str]] = []
    for row in rows:
        try:
            entity_id = aliases.get(normalize_typed_alias(str(row["subject_entity_id"] or "")))
        except ValueError:
            entity_id = None
        item = {"id": str(row["id"]), "text": str(row["index_text"] or _decode(row["value_json"]))}
        (groups[entity_id] if entity_id else unresolved).append(item)
    return dict(groups), unresolved


def _rotated(items: Sequence[dict[str, str]], start: int, count: int) -> list[dict[str, str]]:
    if not items:
        raise ValueError("E4 snapshot group is empty")
    return [dict(items[(start + offset) % len(items)]) for offset in range(count)]


def _e4_candidates(
    targets: Sequence[dict[str, str]], distractors: Sequence[dict[str, str]], target_entity: str, distractor_entity: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for target, distractor in zip(targets, distractors, strict=True):
        result.extend(
            (
                {"claim_id": distractor["id"], "entity_ids": [distractor_entity]},
                {"claim_id": target["id"], "entity_ids": [target_entity]},
            )
        )
    return result


def build_e4_v2_manifest(v1_path: str | Path, database_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    v1 = json.loads(Path(v1_path).read_text(encoding="utf-8"))
    groups, unresolved = _e4_claim_groups(database_path)
    entity_ids = sorted(groups)
    if len(entity_ids) < 4 or len(unresolved) < 50:
        raise ValueError("E4 snapshot lacks concrete entity or no-entity claims")
    aliases_by_entity: dict[str, str] = {}
    for alias in typed_builtin_seeds().aliases:
        aliases_by_entity.setdefault(alias.canonical_entity_id, alias.alias)
    cases: list[dict[str, Any]] = []
    counters: dict[str, int] = defaultdict(int)
    for raw in v1["cases"]:
        category = str(raw["category"])
        index = counters[category]
        counters[category] += 1
        if category == "unique_alias":
            target_entity = entity_ids[index % len(entity_ids)]
            distractor_entity = entity_ids[(index + 1) % len(entity_ids)]
            targets, distractors = _rotated(groups[target_entity], index, 5), _rotated(
                groups[distractor_entity], index, 5
            )
            case = derive_e4_case(category, index, target_entity, [item["id"] for item in targets])
            case["input"]["query"] = f"{aliases_by_entity[target_entity]} {targets[0]['text'][:48]}"
            case["input"]["resolution_class"] = "high"
            case["input"]["candidates"] = _e4_candidates(targets, distractors, target_entity, distractor_entity)
        elif category == "ambiguous":
            left, right = entity_ids[index % len(entity_ids)], entity_ids[(index + 1) % len(entity_ids)]
            targets, distractors = _rotated(groups[left], index, 5), _rotated(groups[right], index, 5)
            case = derive_e4_case(category, index, None, [item["id"] for item in targets + distractors])
            case["input"].update(
                {
                    "query": f"共享实体 {targets[0]['text'][:48]}",
                    "resolution_class": "ambiguous",
                    "resolved_entity_ids": [left, right],
                    "candidates": _e4_candidates(targets, distractors, left, right),
                }
            )
        elif category == "multi_entity":
            left, right = entity_ids[index % len(entity_ids)], entity_ids[(index + 1) % len(entity_ids)]
            targets, distractors = _rotated(groups[left], index, 5), _rotated(groups[right], index, 5)
            case = derive_e4_case(category, index, None, [item["id"] for item in targets + distractors])
            case["input"].update(
                {
                    "query": f"{aliases_by_entity[left]} {aliases_by_entity[right]}",
                    "resolution_class": "multi",
                    "resolved_entity_ids": [left, right],
                    "candidates": _e4_candidates(targets, distractors, left, right),
                }
            )
        else:
            targets = _rotated(unresolved, index, 5)
            case = derive_e4_case(category, index, None, [item["id"] for item in targets])
            case["input"].update(
                {
                    "query": targets[0]["text"][:64] or f"unresolved query {index}",
                    "resolution_class": "low",
                    "candidates": [{"claim_id": item["id"], "entity_ids": []} for item in targets],
                }
            )
        case["input"]["query_id_hash"] = hashlib.sha256(case["input"]["query"].encode()).hexdigest()
        cases.append(case)
    manifest = _seal(
        "E4",
        cases,
        [_snapshot("e4_v1_manifest", v1_path), _snapshot("local_snapshot", database_path)],
        {
            "production_audit_ranked_rows": 2014,
            "usable_production_query_text_rows": 0,
            "snapshot_derived_cases": len(cases),
            "synthetic_query_cases": len(cases),
            "synthetic_query_ratio": 1.0,
            "recommended_synthetic_ratio_max": 0.5,
            "evidence_grade": "LOW_SYNTHETIC_OVER_LIMIT",
            "session_context_included": False,
        },
        {
            "arms": {"A": "wide", "B": "rewrite_only", "C": "high_filter_low_wide"},
            "entity_precision_at_5_improvement_min": 0.10,
            "recall_at_5_drop_max": 0.02,
            "total_recall_drop_max": 0.01,
            "ambiguous_hard_filter": 0,
            "high_confidence_empty_result_increment_max": 0.02,
            "synthetic_ratio_max": 0.5,
        },
    )
    write_manifest(output_path, manifest)
    return manifest


def build_batch4_v2_manifests(
    manifest_dir: str | Path,
    database_path: str | Path,
    volcano_path: str | Path,
) -> dict[str, str]:
    root = Path(manifest_dir)
    outputs = {"E2": root / "e2_v2_preregistered.json", "E3": root / "e3_v2.json", "E4": root / "e4_v2.json"}
    build_e2_v2_preregistration(root / "e2.json", database_path, volcano_path, outputs["E2"])
    build_e3_v2_manifest(root / "e3.json", outputs["E3"])
    build_e4_v2_manifest(root / "e4.json", database_path, outputs["E4"])
    return {key: _sha256(path) for key, path in outputs.items()}
