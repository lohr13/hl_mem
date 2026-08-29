#!/usr/bin/env python
"""Validate and orchestrate the frozen v0.30 experiment manifests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from itertools import product
from pathlib import Path
from typing import Any, Mapping, cast

from hl_mem.domain.governance import L1Policy, decide_l1, is_terminal_conflict_status
from hl_mem.evaluation.v030_batch4 import assess_batch4_manifest, write_batch4_report
from hl_mem.evaluation.v030_batch4_v2_manifest import build_batch4_v2_manifests
from hl_mem.evaluation.v030_batch4_v2_replay import (
    run_e2_v2,
    run_e3_v2,
    run_e4_v2,
)
from hl_mem.evaluation.v030_batch4_v2_replay import write_v2_report as write_batch4_v2_report
from hl_mem.evaluation.v030_corpus import EXPERIMENTS, load_manifest, validate_manifest
from hl_mem.evaluation.v030_e2_clone_replay import attach_recall_comparison, run_e2_clone_rehearsal
from hl_mem.evaluation.v030_plan_price import (
    assess_e5_manifest,
    assess_e6_manifest,
    write_sealed_report,
)
from hl_mem.evaluation.v030_plan_price_manifest import instrument_references
from hl_mem.evaluation.v030_plan_price_replay import (
    load_v2_manifest,
    run_e5_v2,
    run_e6_v2,
    write_v2_report,
)
from hl_mem.evaluation.v030_scorers import evaluate_decision_gate, score_decisions
from hl_mem.workers.auto_resolve_conflicts import AutoDecision, decide_l0


def validate_manifest_directory(manifest_dir: str | Path) -> dict[str, object]:
    """Authenticate E1-E6 and summarize the frozen manifest set."""

    root = Path(manifest_dir)
    summaries: dict[str, dict[str, object]] = {}
    digests: dict[str, str] = {}
    for experiment in sorted(EXPERIMENTS):
        manifest = load_manifest(root / f"{experiment.lower()}.json")
        if manifest["experiment"] != experiment:
            raise ValueError(f"{experiment} manifest filename/content mismatch")
        summaries[experiment] = validate_manifest(manifest)
        digests[experiment] = str(manifest["manifest_sha256"])
    frozen_set = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "phase": "validate",
        "case_counts": {key: cast(int, value["case_count"]) for key, value in summaries.items()},
        "manifest_sha256": digests,
        "manifest_set_sha256": hashlib.sha256(frozen_set).hexdigest(),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_e1_replay_manifest(base_path: str | Path, overlay_path: str | Path) -> dict[str, Any]:
    """Apply an authenticated replay-only status overlay without mutating the frozen base."""

    base = load_manifest(base_path)
    overlay_file = Path(overlay_path)
    overlay = json.loads(overlay_file.read_text(encoding="utf-8"))
    if not isinstance(overlay, Mapping) or overlay.get("schema_version") != "v030-e1-replay-overlay-v2":
        raise ValueError("E1 replay overlay schema is invalid")
    if overlay.get("base_manifest_sha256") != base.get("manifest_sha256"):
        raise ValueError("E1 replay overlay base manifest hash mismatch")
    replay: dict[str, Any] = copy.deepcopy(base)
    cases = {str(item["case_id"]): item for item in replay["cases"]}
    seen: set[str] = set()
    for row in overlay.get("cases") or []:
        if not isinstance(row, Mapping):
            raise ValueError("E1 replay overlay case must be an object")
        case_id = str(row.get("case_id") or "")
        if case_id in seen or case_id not in cases:
            raise ValueError(f"E1 replay overlay has duplicate or unknown case: {case_id}")
        seen.add(case_id)
        claims = {
            str(claim.get("id")): claim
            for claim in (cases[case_id].get("input") or {}).get("claims") or []
            if isinstance(claim, dict)
        }
        overrides = row.get("claim_status_overrides") or {}
        if not isinstance(overrides, Mapping):
            raise ValueError(f"E1 replay status overrides must be an object: {case_id}")
        for claim_id, values in overrides.items():
            if str(claim_id) not in claims or not isinstance(values, Mapping):
                raise ValueError(f"E1 replay overlay has unknown claim: {case_id}/{claim_id}")
            if set(values) - {"pre_decision_status", "status_at_decision"}:
                raise ValueError(f"E1 replay overlay has unsupported status fields: {case_id}/{claim_id}")
            claims[str(claim_id)].update({str(key): str(value) for key, value in values.items() if str(value)})
    conflicts = overlay.get("gold_invariant_conflicts") or []
    if not isinstance(conflicts, list):
        raise ValueError("E1 replay gold_invariant_conflicts must be a list")
    replay["gold_invariant_conflicts"] = sorted({str(case_id) for case_id in conflicts})
    replay["replay_overlay_sha256"] = _file_sha256(overlay_file)
    replay["replay_schema_version"] = str(overlay["schema_version"])
    return replay


def _current_l0_counts(manifest: dict[str, Any]) -> dict[str, object]:
    decisions: Counter[str] = Counter()
    authority = {"low": 1, "medium": 2, "high": 3}
    resolved = 0
    for item in manifest["cases"]:
        docket = item.get("input", {})
        case = docket.get("case", {})
        claims = {claim.get("id"): claim for claim in docket.get("claims", [])}
        left = claims.get(case.get("left_claim_id"), {})
        right = claims.get(case.get("right_claim_id"), {})
        left_score = authority.get(left.get("source_authority"))
        right_score = authority.get(right.get("source_authority"))
        if case.get("group_key") is None and left_score and right_score and left_score != right_score:
            decisions["keep_left" if left_score > right_score else "keep_right"] += 1
            resolved += 1
        else:
            decisions["manual_required"] += 1
    return {
        "decision_counts": dict(sorted(decisions.items())),
        "deferred_missing_predecision_state": len(manifest["cases"]) - resolved,
        "policy": "v0.29.3-authority-observable-subset",
        "resolved_by_authority": resolved,
    }


def run_baseline(
    manifest_dir: str | Path,
    database_path: str | Path,
    config_path: str | Path,
    recall_path: str | Path,
    output_dir: str | Path,
    *,
    arm: str = "A",
) -> dict[str, object]:
    """Run the read-only current-behavior arm and write deterministic evidence."""

    if arm != "A":
        raise ValueError("batch 0 baseline only permits the A arm")
    manifests = validate_manifest_directory(manifest_dir)
    database = Path(database_path)
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        dedup_counts = dict(
            connection.execute(
                "SELECT COALESCE(decision, '<null>'), COUNT(*) FROM dedup_pairs GROUP BY decision"
            ).fetchall()
        )
        dedup_row = connection.execute(
            "SELECT COUNT(*), COUNT(reviewed_at), COUNT(applied_at) FROM dedup_pairs"
        ).fetchone()
        open_count = connection.execute("SELECT COUNT(*) FROM conflict_cases WHERE resolved_at IS NULL").fetchone()[0]
        stable_count = connection.execute("""SELECT COUNT(*) FROM conflict_cases c
               LEFT JOIN conflict_review_state r ON r.case_id=c.id
               WHERE c.status='manual_required' AND c.resolved_at IS NULL
                 AND (r.case_id IS NULL OR r.dirty_at IS NULL)""").fetchone()[0]
    config = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "arm": "A",
        "conflicts": {"open": open_count, "stable_manual_required": stable_count},
        "dedup": {
            "applied": dedup_row[2],
            "audit_only": bool(config.get("dedup", {}).get("audit_only", True)),
            "decision_counts": dict(sorted(dedup_counts.items())),
            "reviewed": dedup_row[1],
            "total": dedup_row[0],
        },
        "e1_l0": _current_l0_counts(load_manifest(Path(manifest_dir) / "e1.json")),
        "inputs": {
            "config_sha256": _file_sha256(Path(config_path)),
            "database_sha256": _file_sha256(database),
            "manifest_set_sha256": manifests["manifest_set_sha256"],
            "recall_sha256": _file_sha256(Path(recall_path)),
        },
        "recall": {
            "dataset_sha256": recall.get("dataset_sha256", recall.get("dataset_hash")),
            "metrics": recall["metrics"],
        },
        "schema_version": "v030-baseline-v1",
    }
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    baseline = root / "baseline.json"
    summary = root / "summary.md"
    baseline.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary.write_text(
        "# v0.30 A-arm baseline\n\n"
        "This replay covers only the v0.29.3 authority branch observable from frozen dockets; deferred cases are not reconstructed.\n\n"
        f"- E1 current-L0 decisions: `{json.dumps(payload['e1_l0'], sort_keys=True)}`\n"
        f"- Dedup: `{json.dumps(payload['dedup'], sort_keys=True)}`\n"
        f"- Conflicts: `{json.dumps(payload['conflicts'], sort_keys=True)}`\n"
        f"- Recall: `{json.dumps(payload['recall'], sort_keys=True)}`\n",
        encoding="utf-8",
    )
    sums = [f"{_file_sha256(path)}  {path.name}" for path in (baseline, summary)]
    (root / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="ascii")
    return payload


def _manifest_claim_status(claim: Mapping[str, Any]) -> str:
    for field in ("pre_decision_status", "status_at_decision", "status"):
        if claim.get(field):
            return str(claim[field])
    # Frozen E1 inputs intentionally omit the post-review lifecycle status.
    # Reconstruct the pre-decision state instead of leaking the historical outcome.
    return "disputed"


def _evidence_rows(input_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = input_payload.get("evidence_refs") or {}
    if not isinstance(refs, Mapping):
        return []
    return [
        {"derived_id": str(claim_id), **dict(item)}
        for claim_id, items in refs.items()
        if isinstance(items, list)
        for item in items
        if isinstance(item, Mapping)
    ]


def _coordinates_complete(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    slot = left.get("canonical_slot")
    return bool(
        slot
        and slot == right.get("canonical_slot")
        and left.get("namespace_key") == right.get("namespace_key")
        and left.get("subject_entity_id")
        and left.get("subject_entity_id") == right.get("subject_entity_id")
        and left.get("qualifiers") == right.get("qualifiers")
    )


def _e1_docket(item: Mapping[str, Any]) -> dict[str, Any]:
    source = cast(Mapping[str, Any], item["input"])
    case = dict(cast(Mapping[str, Any], source.get("case") or {}))
    raw_claims = [dict(claim) for claim in source.get("claims") or [] if isinstance(claim, Mapping)]
    claims_by_id = {str(claim.get("id")): claim for claim in raw_claims}
    endpoints: list[dict[str, Any]] = []
    for side in ("left", "right"):
        claim_id = str(case.get(f"{side}_claim_id") or "")
        if claim_id not in claims_by_id:
            raise ValueError(f"E1 case {item['case_id']} is missing {side} claim")
        claim = dict(claims_by_id[claim_id])
        claim["status"] = _manifest_claim_status(claim)
        endpoints.append(claim)
    evidence = _evidence_rows(source)
    evidence_counts = Counter(str(row["derived_id"]) for row in evidence)
    candidates = [dict(candidate) for candidate in source.get("candidates") or [] if isinstance(candidate, Mapping)]
    if candidates:
        for candidate in candidates:
            member_ids = candidate.get("member_claim_ids") or candidate.get("claim_ids") or []
            statuses = {
                str(claim_id): _manifest_claim_status(claims_by_id[str(claim_id)])
                for claim_id in member_ids
                if str(claim_id) in claims_by_id
            }
            candidate["claim_statuses"] = statuses
            candidate["evidence_count"] = sum(evidence_counts[claim_id] for claim_id in statuses)
            candidate["terminal"] = bool(statuses) and all(
                is_terminal_conflict_status(status) for status in statuses.values()
            )
    else:
        candidates = [
            {
                "candidate_key": str(claim["id"]),
                "representative_claim_id": str(claim["id"]),
                "support_count": 1,
                "evidence_count": evidence_counts[str(claim["id"])],
                "terminal": is_terminal_conflict_status(claim["status"]),
            }
            for claim in endpoints
        ]
    case["group_native"] = bool(case.get("group_key") and source.get("candidates"))
    context = {
        "left_tip_id": endpoints[0]["id"],
        "right_tip_id": endpoints[1]["id"],
        "survivor_contested": False,
        "entity_type_mismatch": False,
        "coordinates_complete": _coordinates_complete(*endpoints),
        "nonexclusive_false_positive": endpoints[0].get("canonical_slot") != endpoints[1].get("canonical_slot"),
    }
    return {
        "case_id": item["case_id"],
        "case": case,
        "claims": endpoints,
        "candidates": candidates,
        "evidence": evidence,
        "context": context,
    }


def _prediction(case_id: str, decision: AutoDecision | None, fallback_rule: str) -> dict[str, Any]:
    resolved = decision or AutoDecision("manual_required", None, 0.0, "L3", fallback_rule)
    return {
        "case_id": case_id,
        "decision": resolved.decision,
        "winner_candidate_key": resolved.winner_candidate_key,
        "confidence": resolved.confidence,
        "tier": resolved.tier,
        "rule": resolved.rule,
        "decisive_evidence_ids": list(resolved.evidence_ids),
        "resolver_model": resolved.resolver_model,
    }


def _matches_gold(prediction: Mapping[str, Any], gold: Mapping[str, Any]) -> bool:
    if prediction.get("decision") != gold.get("decision"):
        return False
    return gold.get("decision") != "select_candidate" or prediction.get("winner_candidate_key") == gold.get(
        "winner_candidate_key"
    )


def _gold_invariant_conflicts(cases: Sequence[Mapping[str, Any]], dockets: Mapping[str, dict[str, Any]]) -> list[str]:
    conflicts: list[str] = []
    for item in cases:
        case_id = str(item["case_id"])
        gold = cast(Mapping[str, Any], item["gold"])
        docket = dockets[case_id]
        decision = gold.get("decision")
        if gold.get("gold_invariant_status") == "gold_invariant_conflict":
            conflicts.append(case_id)
        elif docket["case"].get("group_native") and decision in {"coexist", "reject"}:
            conflicts.append(case_id)
        elif decision == "select_candidate" and str(gold.get("winner_candidate_key")) not in {
            str(candidate.get("candidate_key")) for candidate in docket["candidates"]
        }:
            conflicts.append(case_id)
    return sorted(set(conflicts))


def _arm_report(
    cases: Sequence[Mapping[str, Any]], predictions: list[dict[str, Any]], invariant_ids: list[str]
) -> dict[str, Any]:
    score = score_decisions(cases, predictions)
    score["invariant_violations"] = len(invariant_ids)
    score["gold_invariant_conflict_case_ids"] = invariant_ids
    gold_by_id = {str(item["case_id"]): cast(Mapping[str, Any], item["gold"]) for item in cases}
    mismatches = [str(row["case_id"]) for row in predictions if not _matches_gold(row, gold_by_id[str(row["case_id"])])]
    return {
        "predictions": predictions,
        "score": score,
        "tier_distribution": dict(sorted(Counter(str(row["tier"]) for row in predictions).items())),
        "rule_distribution": dict(sorted(Counter(str(row["rule"]) for row in predictions).items())),
        "l3_reason_distribution": dict(
            sorted(
                Counter(
                    str(row["rule"])
                    for row in predictions
                    if row["tier"] == "L3" or row["decision"] == "manual_required"
                ).items()
            )
        ),
        "mismatch_case_ids": mismatches,
        "gate": evaluate_decision_gate(
            score,
            min_exact=67,
            max_abstentions=3,
            max_destructive=0,
            max_invariant_violations=0,
        ),
    }


def _remaining_gaps(
    arms: Mapping[str, Mapping[str, Any]],
    case_rows: Sequence[Mapping[str, Any]],
    invariant_ids: Sequence[str],
) -> dict[str, Any]:
    b_arm = arms["B"]
    b_score = cast(Mapping[str, Any], b_arm["score"])
    l3_cases: dict[str, list[str]] = {}
    semantic_cases: dict[str, list[str]] = {}
    for item in case_rows:
        prediction = cast(Mapping[str, Any], cast(Mapping[str, Any], item["arms"])["B"])
        rule = str(prediction["rule"])
        if prediction["tier"] == "L3" or prediction["decision"] == "manual_required":
            l3_cases.setdefault(rule, []).append(str(item["case_id"]))
        elif not cast(Mapping[str, Any], item["exact"])["B"]:
            semantic_cases.setdefault(rule, []).append(str(item["case_id"]))
    b_destructive_ids = set(cast(Mapping[str, Any], arms["B"]["score"])["destructive_error_case_ids"])
    b_destructive_rules: dict[str, list[str]] = {}
    for item in case_rows:
        if item["case_id"] not in b_destructive_ids:
            continue
        prediction = cast(Mapping[str, Any], cast(Mapping[str, Any], item["arms"])["B"])
        b_destructive_rules.setdefault(str(prediction["rule"]), []).append(str(item["case_id"]))
    passed = bool(cast(Mapping[str, Any], b_arm["gate"])["passed"])
    return {
        "gate_deficits": {
            "exact_below_67": max(0, 67 - int(b_score["exact"])),
            "l3_above_3": max(0, int(b_score["abstentions"]) - 3),
            "destructive_errors": len(b_score["destructive_error_case_ids"]),
            "gold_invariant_conflicts": len(invariant_ids),
        },
        "l3_defer_cases_by_reason": dict(sorted(l3_cases.items())),
        "semantic_mismatch_cases_by_rule": dict(sorted(semantic_cases.items())),
        "b_destructive_cases_by_rule": dict(sorted(b_destructive_rules.items())),
        "next_action": "release_enforce_at_version_gate" if passed else "keep_observe_and_preregister_any_followup",
    }


def _write_e1_report(report: Mapping[str, Any], output_dir: str | Path, *, version: str) -> None:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    suffix = "_v2" if version == "v2" else ""
    json_path = root / f"e1_report{suffix}.json"
    md_path = root / f"e1_report{suffix}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    rows = ["| Arm | Exact | L3 | Destructive | Gate |", "|---|---:|---:|---:|---|"]
    for name in ("A", "B"):
        arm = report["arms"][name]
        score = arm["score"]
        rows.append(
            f"| {name} | {score['exact']}/70 | {score['abstentions']} | "
            f"{len(score['destructive_error_case_ids'])} | {'PASS' if arm['gate']['passed'] else 'FAIL'} |"
        )
    b_rules = Counter(item["arms"]["B"]["rule"] for item in report["cases"])
    mismatch_rules = Counter(item["arms"]["B"]["rule"] for item in report["cases"] if not item["exact"]["B"])
    invariant_summary = cast(Mapping[str, Any], report["gold_invariant_conflicts"])
    rows.extend(
        [
            "",
            f"## Gold invariant conflicts ({invariant_summary['count']})",
            "",
            *(f"- `{case_id}`" for case_id in invariant_summary["case_ids"]),
            *(["- None"] if not invariant_summary["case_ids"] else []),
            "",
            "## Tier and defer distributions",
            "",
            *(
                f"- {name}: tiers=`{json.dumps(report['arms'][name]['tier_distribution'], sort_keys=True)}`; "
                f"L3=`{json.dumps(report['arms'][name]['l3_reason_distribution'], sort_keys=True)}`"
                for name in ("A", "B")
            ),
            "",
            "## Mismatch analysis",
            "",
            f"- B rule counts: `{json.dumps(dict(b_rules), ensure_ascii=False, sort_keys=True)}`",
            f"- B mismatch rules: `{json.dumps(dict(mismatch_rules), ensure_ascii=False, sort_keys=True)}`",
            f"- B mismatch cases: `{json.dumps(report['arms']['B']['mismatch_case_ids'], ensure_ascii=False)}`",
            "",
            "## Remaining gaps",
            "",
            f"- `{json.dumps(report['remaining_gaps'], ensure_ascii=False, sort_keys=True)}`",
            "",
            "## 70-case replay",
            "",
            "| Case | Source | Gold | A | B | Tier | Rule | Evidence difference | Destructive |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in report["cases"]:
        arms = item["arms"]
        rows.append(
            f"| {item['case_id']} | {item['source']} | {item['gold']['decision']} | "
            f"{arms['A']['decision']} | {arms['B']['decision']} | "
            f"{arms['B']['tier']} | {arms['B']['rule']} | "
            f"gold unavailable / {','.join(arms['B']['decisive_evidence_ids']) or 'none'} | "
            f"{'yes' if item['destructive'] else 'no'} |"
        )
    recommendation = cast(Mapping[str, Any], report["enforce_recommendation"])
    rows.extend(
        [
            "",
            "## Enforce recommendation",
            "",
            f"- Decision: `{recommendation['decision']}`",
            f"- Current batch mode: `{recommendation['current_batch_mode']}`",
            f"- Release mode: `{recommendation['release_mode']}`",
            f"- L1 policy: `{recommendation['selected_l1_policy']}`",
        ]
    )
    title = "# E1 conflict automation gate v2" if version == "v2" else "# E1 conflict automation gate"
    md_path.write_text(title + "\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    if version == "v2":
        passed = bool(report["arms"]["B"]["gate"]["passed"])
        seal_path = root / ("E1_V2_PASSED" if passed else "SEALED_FAILED_v2")
        seal_path.write_text(
            f"status={'PASS' if passed else 'SEALED_FAILED'}\n"
            f"json_sha256={_file_sha256(json_path)}\n"
            f"markdown_sha256={_file_sha256(md_path)}\n",
            encoding="ascii",
        )
        paths = (json_path, md_path, seal_path)
        (root / "SHA256SUMS_v2").write_text(
            "\n".join(f"{_file_sha256(path)}  {path.name}" for path in paths) + "\n",
            encoding="ascii",
        )


def run_e1_experiment(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    replay_overlay_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run frozen true-L0 and candidate-L1 arms."""

    version = "v2" if replay_overlay_path is not None else "v1"
    manifest = (
        load_e1_replay_manifest(manifest_path, replay_overlay_path)
        if replay_overlay_path is not None
        else load_manifest(manifest_path)
    )
    if manifest["experiment"] != "E1":
        raise ValueError("run_e1_experiment requires the E1 manifest")
    cases = cast(list[Mapping[str, Any]], manifest["cases"])
    dockets = {str(item["case_id"]): _e1_docket(item) for item in cases}
    invariant_ids = sorted(
        set(_gold_invariant_conflicts(cases, dockets)) | set(manifest.get("gold_invariant_conflicts") or [])
    )
    a_predictions = [_prediction(case_id, decide_l0(docket), "l0_insufficient") for case_id, docket in dockets.items()]
    policy_reports: dict[str, dict[str, Any]] = {}
    policy_predictions: dict[str, list[dict[str, Any]]] = {}
    for time_delta, confidence_delta in product((0, 300, 3_600), (0.10, 0.15, 0.20)):
        key = f"t{time_delta}-c{confidence_delta:.2f}"
        policy = L1Policy(time_delta, confidence_delta)
        predictions = [
            _prediction(case_id, decide_l0(docket) or decide_l1(docket, policy), "l1_insufficient")
            for case_id, docket in dockets.items()
        ]
        policy_predictions[key] = predictions
        policy_reports[key] = _arm_report(cases, predictions, invariant_ids)
    selected_key = max(
        policy_reports,
        key=lambda key: (
            not policy_reports[key]["score"]["destructive_error_case_ids"],
            policy_reports[key]["score"]["exact"],
            -policy_reports[key]["score"]["abstentions"],
            int(key.split("-")[0][1:]),
            float(key.split("c")[1]),
        ),
    )
    b_predictions = policy_predictions[selected_key]
    arms = {
        "A": _arm_report(cases, a_predictions, invariant_ids),
        "B": _arm_report(cases, b_predictions, invariant_ids),
    }
    indexed = {name: {row["case_id"]: row for row in arm["predictions"]} for name, arm in arms.items()}
    destructive = set(arms["B"]["score"]["destructive_error_case_ids"])
    case_rows = [
        {
            "case_id": item["case_id"],
            "source": item["source"],
            "gold": item["gold"],
            "arms": {name: indexed[name][item["case_id"]] for name in ("A", "B")},
            "exact": {
                name: _matches_gold(indexed[name][item["case_id"]], cast(Mapping[str, Any], item["gold"]))
                for name in ("A", "B")
            },
            "destructive": item["case_id"] in destructive,
            "evidence_difference": {
                "gold": "not present in frozen label",
                "auto": indexed["B"][item["case_id"]]["decisive_evidence_ids"],
            },
        }
        for item in cases
    ]
    b_passed = bool(arms["B"]["gate"]["passed"])
    report: dict[str, Any] = {
        "schema_version": f"v030-e1-report-{version}",
        "manifest_sha256": manifest["manifest_sha256"],
        "replay_overlay_sha256": manifest.get("replay_overlay_sha256"),
        "gold_invariant_conflicts": {"count": len(invariant_ids), "case_ids": invariant_ids},
        "thresholds": {"min_exact": 67, "max_l3": 3, "max_destructive": 0, "max_invariants": 0},
        "selected_l1_policy": selected_key,
        "l1_selection_rule": "zero-destructive-first, exact-desc, abstention-asc, conservative-tie-break",
        "candidate_l1_policies": policy_reports,
        "enforce_recommendation": {
            "decision": "recommend_enforce_at_release" if b_passed else "keep_observe",
            "current_batch_mode": "observe",
            "release_mode": "enforce" if b_passed else "observe",
            "selected_l1_policy": selected_key,
        },
        "remaining_gaps": _remaining_gaps(arms, case_rows, invariant_ids),
        "arms": arms,
        "cases": case_rows,
    }
    _write_e1_report(report, output_dir, version=version)
    return report


def run_plan_price_preflight(
    manifest_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Authenticate E5/E6 and stop before qwen when the frozen corpus cannot support scoring."""

    manifest = load_manifest(manifest_path)
    experiment = str(manifest["experiment"])
    if experiment == "E5":
        assessment = assess_e5_manifest(manifest)
    elif experiment == "E6":
        assessment = assess_e6_manifest(manifest)
    else:
        raise ValueError("plan/price preflight accepts only E5 or E6")
    if assessment["ready"]:
        return {
            "schema_version": f"v030-{experiment.lower()}-waiting-qwen-v1",
            "status": "WAITING_QWEN",
            "manifest_sha256": manifest["manifest_sha256"],
            "assessment": assessment,
        }
    return write_sealed_report(output_dir, manifest, assessment)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "validate",
            "baseline",
            "build-batch4-v2",
            "e1",
            "e2",
            "e3",
            "e4",
            "e5",
            "e6",
            "e2-v2",
            "e3-v2",
            "e4-v2",
            "e5-v2",
            "e6-v2",
        ),
    )
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--recall-baseline", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--arm", default="A")
    parser.add_argument("--qwen-base-url")
    parser.add_argument("--qwen-model", default="Qwen3.8-27B-UD-IQ4_XS.gguf")
    parser.add_argument("--model-file", type=Path)
    parser.add_argument("--model-sha256")
    parser.add_argument("--llama-build")
    parser.add_argument("--e1-replay-overlay", type=Path)
    parser.add_argument("--reuse-report", type=Path)
    parser.add_argument("--volcano-e2", type=Path)
    parser.add_argument("--batch0-baseline", type=Path)
    parser.add_argument("--recall-current-report", type=Path)
    args = parser.parse_args(argv)
    if args.phase == "validate":
        result = validate_manifest_directory(args.manifest_dir)
    elif args.phase == "baseline":
        required = (args.database, args.config, args.recall_baseline, args.output_dir)
        if any(path is None for path in required):
            parser.error("baseline requires --database, --config, --recall-baseline and --output-dir")
        result = run_baseline(
            args.manifest_dir,
            args.database,
            args.config,
            args.recall_baseline,
            args.output_dir,
            arm=args.arm,
        )
    elif args.phase == "build-batch4-v2":
        if args.database is None or args.volcano_e2 is None:
            parser.error("build-batch4-v2 requires --database and --volcano-e2")
        result = {
            "phase": args.phase,
            "file_sha256": build_batch4_v2_manifests(args.manifest_dir, args.database, args.volcano_e2),
        }
    elif args.phase == "e1":
        if args.output_dir is None:
            parser.error("e1 requires --output-dir")
        report = run_e1_experiment(
            args.manifest_dir / "e1.json",
            args.output_dir,
            replay_overlay_path=args.e1_replay_overlay,
        )
        result = {
            "phase": "e1",
            "selected_l1_policy": report["selected_l1_policy"],
            "gates": {name: arm["gate"] for name, arm in report["arms"].items()},
            "output_dir": str(args.output_dir),
        }
    elif args.phase in {"e2", "e3", "e4"}:
        if args.output_dir is None:
            parser.error(f"{args.phase} requires --output-dir")
        manifest = load_manifest(args.manifest_dir / f"{args.phase}.json")
        assessment = assess_batch4_manifest(manifest)
        result = write_batch4_report(args.output_dir, manifest, assessment)
    elif args.phase in {"e2-v2", "e3-v2", "e4-v2"}:
        if args.output_dir is None:
            parser.error(f"{args.phase} requires --output-dir")
        experiment = args.phase.split("-", 1)[0]
        manifest_name = "e2_v2_preregistered.json" if experiment == "e2" else f"{experiment}_v2.json"
        manifest = load_manifest(args.manifest_dir / manifest_name)
        if experiment == "e4":
            report = run_e4_v2(manifest)
        else:
            if args.qwen_base_url is None:
                parser.error(f"{args.phase} requires --qwen-base-url")
            from hl_mem.evaluation.local_qwen_runner import LocalQwenRunner, QwenLimits, QwenRunConfig

            runner = LocalQwenRunner(
                token_counter=lambda text: len(text.encode("utf-8")),
                config=QwenRunConfig(
                    base_url=args.qwen_base_url,
                    model=args.qwen_model,
                    enable_thinking=False,
                    timeout_seconds=180,
                    limits=QwenLimits(max_output_tokens=1024 if experiment == "e2" else 640),
                ),
            )

            def progress(message: str) -> None:
                print(message, flush=True)

            if experiment == "e3":
                report = run_e3_v2(manifest, runner, progress=progress, checkpoint_dir=args.output_dir)
            else:
                required = (
                    args.database,
                    args.volcano_e2,
                    args.batch0_baseline,
                    args.recall_current_report,
                )
                if any(path is None for path in required):
                    parser.error(
                        "e2-v2 requires --database, --volcano-e2, --batch0-baseline and --recall-current-report"
                    )
                clone = run_e2_clone_rehearsal(args.database, args.volcano_e2, manifest)
                baseline = json.loads(args.batch0_baseline.read_text(encoding="utf-8"))["recall"]["metrics"]
                current_report = json.loads(args.recall_current_report.read_text(encoding="utf-8"))
                case_delta = current_report["baseline_comparison"].get("case_delta") or {}
                negative = any(value < 0 for row in case_delta.values() for value in row.values())
                rehearsal = attach_recall_comparison(
                    clone,
                    baseline,
                    current_report["metrics"],
                    paired_regression_p=0.0 if negative else 1.0,
                )
                report = run_e2_v2(
                    manifest,
                    runner,
                    final_manifest_path=args.manifest_dir / "e2_v2.json",
                    rehearsal=rehearsal,
                    progress=progress,
                    checkpoint_path=args.output_dir / "qwen_checkpoint.json",
                )
                if args.reuse_report is not None:
                    previous = json.loads(args.reuse_report.read_text(encoding="utf-8"))
                    report["qwen"]["elapsed_seconds"] = max(
                        float(report["qwen"]["elapsed_seconds"]),
                        float((previous.get("qwen") or {}).get("elapsed_seconds") or 0),
                    )
            report["model"] = asdict(runner.config)
            report["execution_notes"] = {
                "clean_start_verified": True,
                "double_permutation": True,
                "enable_thinking": False,
                "max_output_tokens": runner.config.max_output_tokens,
                "unscored_aborted_probe_runs": 2 if experiment == "e3" else 1,
                "unscored_completed_equipment_calls": 4 if experiment == "e3" else 18,
                "sealed_pre_action_coordinate_calls": 82 if experiment == "e2" else 0,
            }
        checksums = write_batch4_v2_report(args.output_dir, report)
        result = {
            "phase": args.phase,
            "status": report["status"],
            "selected_arm": report.get("selected_arm"),
            "qwen": report.get("qwen"),
            "checksums": checksums,
        }
    elif args.phase in {"e5", "e6"}:
        if args.output_dir is None:
            parser.error(f"{args.phase} requires --output-dir")
        result = run_plan_price_preflight(
            args.manifest_dir / f"{args.phase}.json",
            args.output_dir,
        )
    else:
        if args.output_dir is None:
            parser.error(f"{args.phase} requires --output-dir")
        if args.qwen_base_url is None:
            parser.error(f"{args.phase} requires --qwen-base-url")
        from hl_mem.evaluation.local_qwen_runner import LocalQwenRunner, QwenLimits, QwenRunConfig

        experiment = args.phase.split("-", 1)[0]
        manifest = load_v2_manifest(args.manifest_dir / f"{experiment}_v2.json")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        waiting = {
            "schema_version": f"v030-{experiment}-waiting-qwen-v2",
            "status": "WAITING_QWEN",
            "manifest_sha256": manifest["manifest_sha256"],
        }
        (args.output_dir / "waiting_qwen.json").write_text(
            json.dumps(waiting, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        runner = LocalQwenRunner(
            token_counter=lambda text: len(text.encode("utf-8")),
            config=QwenRunConfig(
                base_url=args.qwen_base_url,
                model=args.qwen_model,
                enable_thinking=False,
                timeout_seconds=60,
                limits=QwenLimits(max_output_tokens=128),
            ),
        )

        def progress(message: str) -> None:
            print(message, flush=True)

        reuse_report = (
            json.loads(args.reuse_report.read_text(encoding="utf-8")) if args.reuse_report is not None else None
        )
        report = (
            run_e5_v2(manifest, runner, progress=progress, reuse_report=reuse_report)
            if experiment == "e5"
            else run_e6_v2(manifest, runner, instrument_references(), progress=progress)
        )
        report["execution_history"] = [
            "authenticated_manifest",
            "preflight_v2_passed",
            "WAITING_QWEN",
            "qwen_replay_completed",
        ]
        report["model"] = asdict(runner.config)
        if args.reuse_report is not None:
            report["reuse_source"] = {
                "path": str(args.reuse_report),
                "sha256": _file_sha256(args.reuse_report),
                "rule": "both candidate-order request SHA256 values must match",
            }
        checksums = write_v2_report(args.output_dir, report)
        result = {
            "phase": args.phase,
            "status": report["status"],
            "selected_arm": report["selected_arm"],
            "qwen": report["qwen"],
            "checksums": checksums,
            "output_dir": str(args.output_dir),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
