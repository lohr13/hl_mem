"""Frozen-manifest builders for the E5/E6 v2 corpus repair replay."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hl_mem.domain.instruments import InstrumentReference
from hl_mem.evaluation.v030_corpus import SCHEMA_VERSION, manifest_sha256, validate_manifest, write_manifest
from hl_mem.evaluation.v030_plan_price_corpus import (
    assess_e5_v2_manifest,
    assess_e6_v2_manifest,
    derive_e5_case,
    derive_e6_pair,
    derive_real_action_claim,
)

_INNOVATION_PLAN_IDS = (
    "3e8382ec4e7d484595a5b60b6b166694",
    "b3eedd903f7045abbe37b882b8114fef",
    "2ec25a92be7449e1a9052c2000aef9ed",
    "a8bda7e3eeca49f68473c3cb3bcecd65",
)
_INNOVATION_RESULT_ID = "944764ff92c04cb1ba5b4e76ba7902e4"
_GOLD_PLAN_IDS = (
    "179f7775a5574f8f9b2fc82eebd2919c",
    "1a72c20d4ca146e0826e95568a639497",
    "201ec51568a44202a80eb130e7236e9f",
)
_GOLD_RESULT_ID = "c5d42912af424b00b63effb1246773de"
_RISK_CORRECTIONS = {
    "e5:complete:001": "quantity_mismatch_5200_vs_10500",
    "e5:complete:002": "negated_result_cannot_complete",
    "e5:complete:003": "cross_account_cannot_complete",
}


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode(value: Any) -> str:
    if not isinstance(value, str):
        return str(value or "")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return str(decoded) if not isinstance(decoded, str) else decoded


def _claim_rows(replica_path: str | Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{Path(replica_path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(claims)")}
        value_column = "value_json" if "value_json" in columns else "value"
        rows = connection.execute(
            f"SELECT id, subject_entity_id, {value_column} AS value, valid_from, predicate FROM claims"
        ).fetchall()
    finally:
        connection.close()
    return {
        str(row["id"]): {
            "id": str(row["id"]),
            "subject_entity_id": str(row["subject_entity_id"] or ""),
            "value": _decode(row["value"]),
            "valid_from": row["valid_from"],
            "predicate": row["predicate"],
        }
        for row in rows
    }


def instrument_references() -> list[InstrumentReference]:
    """Return the frozen typed registry used only by the offline v2 derivation."""

    known = [
        InstrumentReference("instrument:CN:SH:515120", "CN:SH:515120", (("创新药ETF", 1), ("创新药 ETF", 1))),
        InstrumentReference("instrument:CN:SH:518880", "CN:SH:518880", (("黄金ETF", 1), ("黄金", 1))),
        InstrumentReference("instrument:CN:SH:600111", "CN:SH:600111", (("北方稀土", 1), ("北稀", 1))),
        InstrumentReference("instrument:CN:SZ:000998", "CN:SZ:000998", (("隆平高科", 1),)),
        InstrumentReference("instrument:index:000001", "INDEX:CN:000001", (("上证指数", 1), ("上证", 1))),
        InstrumentReference("instrument:index:399006", "INDEX:CN:399006", (("创业板指", 1), ("创业板", 1))),
        InstrumentReference("instrument:US:SKHY", "US:NASDAQ:SKHY", (("NASDAQ:SKHY", 1),)),
    ]
    generated = [
        InstrumentReference(
            f"instrument:synthetic:{index:02d}",
            f"US:NASDAQ:X{index:02d}",
            ((f"NASDAQ:X{index:02d}", 1),),
        )
        for index in range(52)
    ]
    legacy_e5 = [
        InstrumentReference(
            f"instrument:{index:02d}",
            f"US:NASDAQ:P{index:02d}",
            ((f"NASDAQ:P{index:02d}", 1),),
        )
        for index in range(40)
    ]
    return [*known, *legacy_e5, *generated]


def synthetic_e6_cases(
    references: Sequence[InstrumentReference], *, count: int, unresolved_count: int
) -> list[dict[str, Any]]:
    """Build deterministic price pairs, including a preregistered unresolved slice."""

    if not references or unresolved_count > count:
        raise ValueError("synthetic E6 case parameters are invalid")
    cases: list[dict[str, Any]] = []
    for index in range(count):
        reference = references[index % len(references)]
        unresolved = index < unresolved_count
        cross_target = not unresolved and index % 10 == 2
        right_reference = references[(index + 1) % len(references)] if cross_target else reference
        mention = reference.canonical_key.rsplit(":", 1)[-1] if unresolved else reference.aliases[0][0]
        right_mention = (
            right_reference.canonical_key.rsplit(":", 1)[-1] if unresolved else right_reference.aliases[0][0]
        )
        left = {
            "id": f"e6-v2-left-{index:03d}",
            "subject_entity_id": mention,
            "value": f"{mention} close price USD {10 + index}.00 on 2026-08-18",
            "valid_from": "2026-08-18T15:00:00+00:00",
        }
        right = {
            "id": f"e6-v2-right-{index:03d}",
            "subject_entity_id": right_mention,
            "value": f"{right_mention} close price USD {11 + index}.00 on 2026-08-19",
            "valid_from": "2026-08-19T15:00:00+00:00",
        }
        case = derive_e6_pair(
            f"e6-v2:synthetic:{index:03d}",
            left,
            right,
            references,
            source="synthetic_contract_v2",
            label_provenance={"kind": "deterministic_coordinate_contract", "version": "v2"},
        )
        case["instrument_id"] = reference.canonical_entity_id
        case["gold"]["left_target_entity_id"] = reference.canonical_entity_id
        case["gold"]["right_target_entity_id"] = right_reference.canonical_entity_id
        if cross_target:
            case["risk_tags"] = ["cross_target"]
        cases.append(case)
    return cases


def _snapshot(source_id: str, path: str | Path) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "sha256": _sha256(path),
        "reconstructable": True,
        "path_hint": Path(path).name,
    }


def _seal(
    experiment: str,
    cases: Sequence[Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
    *,
    source_audit: Mapping[str, Any],
    preregistration: Mapping[str, Any],
    excluded: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "design_baseline": "2026-08-25",
        "corpus_revision": "v2",
        "experiment": experiment,
        "source_snapshots": sorted((dict(item) for item in snapshots), key=lambda item: item["source_id"]),
        "source_audit": copy.deepcopy(dict(source_audit)),
        "preregistration": copy.deepcopy(dict(preregistration)),
        "excluded_with_reason": [copy.deepcopy(dict(item)) for item in excluded],
        "cases": sorted((copy.deepcopy(dict(case)) for case in cases), key=lambda item: item["case_id"]),
    }
    validate_manifest(manifest)
    manifest["manifest_sha256"] = manifest_sha256(manifest)
    validate_manifest(manifest)
    return manifest


def _real_plan_case(
    case: dict[str, Any], rows: Mapping[str, Mapping[str, Any]], references: Sequence[InstrumentReference]
) -> dict[str, Any]:
    innovation = "innovation_variants" in case.get("risk_tags", [])
    plan_ids = _INNOVATION_PLAN_IDS if innovation else _GOLD_PLAN_IDS
    result_id = _INNOVATION_RESULT_ID if innovation else _GOLD_RESULT_ID
    plans = [derive_real_action_claim(rows[claim_id], references, is_plan=True) for claim_id in plan_ids]
    result = derive_real_action_claim(rows[result_id], references, is_plan=False)
    case["source"] = "volcano_replica"
    case["input"] = {"plan": plans[1 if innovation else 0], "plans": plans, "result": result}
    case["source_claim_ids"] = [*plan_ids, result_id]
    return case


def build_e5_v2_manifest(v1_path: str | Path, replica_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Repair E5 only by deterministic projection and explicit gold corrections."""

    v1 = json.loads(Path(v1_path).read_text(encoding="utf-8"))
    rows = _claim_rows(replica_path)
    references = instrument_references()
    cases: list[dict[str, Any]] = []
    for raw in v1["cases"]:
        case = copy.deepcopy(raw)
        if case["case_id"] in _RISK_CORRECTIONS:
            case["category"] = "ambiguous_negative"
            case["gold"]["decision"] = "ambiguous"
            case["correction_reason"] = _RISK_CORRECTIONS[case["case_id"]]
        elif case["category"] == "ambiguous_negative":
            case["gold"]["decision"] = "ambiguous"
        if "innovation_variants" in case.get("risk_tags", []) or "gold_10500" in case.get("risk_tags", []):
            case = _real_plan_case(case, rows, references)
        else:
            case = derive_e5_case(case, references)
        if "negation" in case.get("risk_tags", []):
            case["input"]["result"]["negated"] = True
            case["input"]["result"]["text"] = f"did not {case['input']['result']['text']}"
        if "cross_account" in case.get("risk_tags", []):
            case["input"]["plan"]["account"] = "account:A"
            case["input"]["result"]["account"] = "account:B"
        if "unordered_partial" in case.get("risk_tags", []):
            result = case["input"]["result"]
            plan_amount = int(case["input"]["plan"]["quantity"])
            first_amount = plan_amount // 2
            case["input"]["partial_results"] = [
                {**result, "claim_id": "unordered:later", "quantity": str(plan_amount - first_amount)},
                {**result, "claim_id": "unordered:earlier", "quantity": str(first_amount)},
            ]
        cases.append(case)
    for index in range(3):
        cases.append(
            derive_e5_case(
                {
                    "case_id": f"e5-v2:supplemental-complete:{index:02d}",
                    "category": "complete",
                    "source": "synthetic_contract_v2",
                    "input": {
                        "plan": {"target": f"instrument:synthetic:{index:02d}", "quantity": "100"},
                        "result": {"target": f"instrument:synthetic:{index:02d}", "quantity": "100"},
                    },
                    "gold": {"decision": "complete"},
                },
                references,
            )
        )
    real_count = sum(case["source"] == "volcano_replica" for case in cases)
    manifest = _seal(
        "E5",
        cases,
        [_snapshot("e5_v1_manifest", v1_path), _snapshot("volcano_full_replica", replica_path)],
        source_audit={
            "real_cases": real_count,
            "synthetic_cases": len(cases) - real_count,
            "gold_corrections": _RISK_CORRECTIONS,
            "derivation": "offline_production_parsers",
        },
        preregistration={
            "max_coordinate_missing_rate": 0.10,
            "error_closures": 0,
            "outcome_macro_f1_min": 0.95,
            "per_outcome_recall_min": 0.90,
            "partial_conservation": 1.0,
            "ambiguous_abstain_recall_min": 0.95,
            "qwen_selection_rule": "all_cases_double_permutation",
        },
        excluded=[
            {
                "source_id": "volcano_hermes_verified_replay_20260821",
                "reason": "v1_source_snapshot_not_reconstructable_replaced_by_full_replica",
            }
        ],
    )
    assessment = assess_e5_v2_manifest(manifest)
    if not assessment["ready"]:
        raise ValueError(f"E5 v2 preflight failed: {assessment['blockers']}")
    write_manifest(output_path, manifest)
    return {"manifest": manifest, "assessment": assessment}


def _conflict_pairs(replica_path: str | Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{Path(replica_path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT cc.id, cc.decision, left_claim_id, right_claim_id
            FROM conflict_cases AS cc
            WHERE cc.status = 'resolved' AND left_claim_id IS NOT NULL AND right_claim_id IS NOT NULL
            ORDER BY cc.id
            """).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _real_e6_cases(replica_path: str | Path, references: Sequence[InstrumentReference]) -> list[dict[str, Any]]:
    claims = _claim_rows(replica_path)
    cases: list[dict[str, Any]] = []
    for row in _conflict_pairs(replica_path):
        left, right = claims.get(str(row["left_claim_id"])), claims.get(str(row["right_claim_id"]))
        if not left or not right:
            continue
        case = derive_e6_pair(
            f"e6-v2:conflict:{row['id']}",
            left,
            right,
            references,
            source="volcano_conflict_case",
            label_provenance={"kind": "conflict_case", "id": row["id"], "decision": row["decision"]},
        )
        sides = case["input"]
        if all(side.get("canonical_target_entity_id") and side.get("price_axis") for side in sides.values()):
            case["gold"]["left_target_entity_id"] = sides["left"]["canonical_target_entity_id"]
            case["gold"]["right_target_entity_id"] = sides["right"]["canonical_target_entity_id"]
            cases.append(case)
    return cases[:40]


def _dedup_e6_cases(evidence_path: str | Path, references: Sequence[InstrumentReference]) -> list[dict[str, Any]]:
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    claims = {
        str(row["id"]): {
            "id": row["id"],
            "subject_entity_id": row.get("subject_entity_id") or "",
            "value": _decode(row.get("value_json")),
            "valid_from": row.get("valid_from"),
        }
        for row in evidence.get("claims", [])
    }
    cases: list[dict[str, Any]] = []
    for pair in evidence.get("pairs", []):
        left = claims.get(str(pair.get("left_claim_id")))
        right = claims.get(str(pair.get("right_claim_id")))
        if not left or not right or pair.get("decision") != "equivalent":
            continue
        case = derive_e6_pair(
            f"e6-v2:dedup:{pair['id']}",
            left,
            right,
            references,
            source="volcano_dedup_pair",
            label_provenance={"kind": "dedup_pair", "id": pair["id"], "decision": "equivalent"},
        )
        sides = case["input"]
        targets = {side.get("canonical_target_entity_id") for side in sides.values()} - {None}
        axes = {side.get("price_axis") for side in sides.values()} - {None}
        if len(targets) != 1 or not axes:
            continue
        if len(axes) == 1:
            for side in sides.values():
                if side.get("price_axis") is None:
                    side["price_axis"] = next(iter(axes))
                    side["axis_proof"] = "dedup_equivalent_peer"
        case["instrument_id"] = next(iter(targets))
        case["gold"]["decision"] = "uncertain"
        case["gold"]["left_target_entity_id"] = next(iter(targets))
        case["gold"]["right_target_entity_id"] = next(iter(targets))
        cases.append(case)
    return cases


def build_e6_v2_manifest(
    v1_path: str | Path,
    replica_path: str | Path,
    dedup_evidence_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """Build E6 from resolved Volcano cases plus bounded deterministic coverage fixtures."""

    references = instrument_references()
    conflict_cases = _real_e6_cases(replica_path, references)
    dedup_cases = _dedup_e6_cases(dedup_evidence_path, references)
    real_cases = [*conflict_cases, *dedup_cases]
    synthetic_references = [item for item in references if ":synthetic:" in item.canonical_entity_id]
    synthetic = synthetic_e6_cases(synthetic_references, count=120 - len(real_cases), unresolved_count=12)
    cases = [*real_cases, *synthetic]
    manifest = _seal(
        "E6",
        cases,
        [
            _snapshot("e6_v1_manifest", v1_path),
            _snapshot("volcano_full_replica", replica_path),
            _snapshot("volcano_e2_dedup_evidence", dedup_evidence_path),
        ],
        source_audit={
            "real_cases": len(real_cases),
            "synthetic_cases": len(synthetic),
            "conflict_case_labels": len(conflict_cases),
            "dedup_pair_labels": len(dedup_cases),
            "replica_dedup_pairs_table": "absent",
            "dedup_decisions_source": Path(dedup_evidence_path).name,
            "derivation": "offline_production_parsers",
        },
        preregistration={
            "max_target_missing_rate": 0.10,
            "exact_target_precision": 1.0,
            "target_coverage_min": 0.90,
            "cross_target_supersede": 0,
            "missing_to_uncertain": 1.0,
            "qwen_selection_rule": "target_missing_cases_double_permutation_audit_only",
        },
        excluded=[
            {
                "source_id": "volcano_hermes_verified_replay_20260821",
                "reason": "v1_source_snapshot_not_reconstructable_replaced_by_full_replica",
            }
        ],
    )
    assessment = assess_e6_v2_manifest(manifest)
    if not assessment["ready"]:
        raise ValueError(f"E6 v2 preflight failed: {assessment['blockers']}")
    write_manifest(output_path, manifest)
    return {"manifest": manifest, "assessment": assessment}
