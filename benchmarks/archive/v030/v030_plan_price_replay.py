"""Three-arm E5/E6 v2 replay with bounded local-Qwen audit calls."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from benchmarks.archive.v030.v030_corpus import load_manifest
from benchmarks.archive.v030.v030_plan_price_corpus import (
    assess_e5_v2_manifest,
    assess_e6_v2_manifest,
)
from hl_mem.domain.instruments import InstrumentReference
from hl_mem.domain.plan_fulfillment import select_plan_match
from hl_mem.evaluation.local_qwen_runner import LocalQwenRunner

Progress = Callable[[str], None]
_E5_OUTCOMES = ("complete", "cancel", "replace", "partial", "ambiguous")


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def deterministic_e5_decision(case: Mapping[str, Any]) -> str:
    """Apply the strict protected-coordinate and Decimal conservation arm."""

    payload = case.get("input") or {}
    plan, result = payload.get("plan") or {}, payload.get("result") or {}
    if result.get("negated"):
        return "ambiguous"

    def claim(row: Mapping[str, Any], claim_id: str, valid_from: str) -> dict[str, Any]:
        qualifiers = {
            "action_family": row.get("action_family"),
            "assertion_phase": row.get("assertion_phase"),
            "direction": row.get("direction"),
            "quantity_mode": row.get("quantity_mode"),
            "quantity": row.get("quantity"),
            "quantity_unit": row.get("unit"),
            "account": row.get("account"),
        }
        return {
            "id": claim_id,
            "namespace_key": "default",
            "canonical_target_entity_id": row.get("canonical_target_entity_id"),
            "valid_from": valid_from,
            "qualifiers": qualifiers,
        }

    match = select_plan_match(
        [claim(plan, "plan", "2026-08-18T00:00:00+00:00")],
        claim(result, "result", "2026-08-19T00:00:00+00:00"),
    )
    return match.outcome_type if match is not None else "ambiguous"


def _wide_e5_decision(case: Mapping[str, Any]) -> str:
    result = (case.get("input") or {}).get("result") or {}
    phase = result.get("assertion_phase")
    if phase == "cancellation":
        return "cancel"
    if phase == "replacement":
        return "replace"
    return "ambiguous" if result.get("negated") else "complete"


def qwen_e5_docket(case: Mapping[str, Any]) -> dict[str, Any]:
    """Return only observable coordinates; frozen gold never enters a model request."""

    payload = case.get("input") or {}
    return {
        "case_id": str(case.get("case_id") or ""),
        "instruction": (
            "Classify plan outcome as complete, cancel, replace, partial, or ambiguous. "
            "Protected target, direction, account, unit and quantity must agree. Overfill, negation, "
            "or incomplete coordinates are ambiguous. Return decision and confidence JSON only."
        ),
        "allowed_decisions": list(_E5_OUTCOMES),
        "candidates": [
            {"candidate_key": "plan", "coordinate": dict(payload.get("plan") or {})},
            {"candidate_key": "result", "coordinate": dict(payload.get("result") or {})},
        ],
        "evidence": [dict(item) for item in payload.get("partial_results") or []],
    }


def qwen_e6_docket(case: Mapping[str, Any], references: Sequence[InstrumentReference]) -> dict[str, Any]:
    """Build an audit-only typed candidate docket from the unresolved visible mention."""

    sides = case.get("input") or {}
    mentions = sorted(
        {
            str(side.get("subject_entity_id") or "").strip()
            for side in sides.values()
            if isinstance(side, Mapping) and side.get("subject_entity_id")
        }
    )
    suffixes = {mention.upper() for mention in mentions}
    candidates = [
        {
            "candidate_key": reference.canonical_entity_id,
            "canonical_key": reference.canonical_key,
            "aliases": [alias for alias, _ in reference.aliases],
        }
        for reference in references
        if reference.canonical_key.rsplit(":", 1)[-1].upper() in suffixes
    ]
    return {
        "case_id": str(case.get("case_id") or ""),
        "instruction": (
            "Audit an unresolved financial-instrument mention. Return decision resolved or unresolved and "
            "confidence. A bare ticker without market is unresolved; never invent an ID. When resolved, "
            "winner_candidate_key must be one listed candidate."
        ),
        "allowed_decisions": ["resolved", "unresolved"],
        "mentions": mentions,
        "candidates": candidates,
        "evidence": [],
    }


def request_hashes_for_docket(docket: Mapping[str, Any], runner: LocalQwenRunner) -> list[str]:
    """Render the exact two requests through a fake transport for safe replay reuse."""

    verifier = LocalQwenRunner(
        token_counter=runner.token_counter,
        transport=lambda _url, _payload: {"decision": "verified", "confidence": 1.0},
        config=runner.config,
    )
    result = verifier.run_case(docket)
    return [str(item) for item in result["request_sha256"]]


def _classification_metrics(
    cases: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    indexed = {str(row["case_id"]): str(row.get("decision") or "<missing>") for row in predictions}
    confusion: dict[str, Counter[str]] = {outcome: Counter() for outcome in _E5_OUTCOMES}
    for case in cases:
        expected = str((case.get("gold") or {}).get("decision"))
        confusion.setdefault(expected, Counter())[indexed.get(str(case["case_id"]), "<missing>")] += 1
    recalls: dict[str, float] = {}
    f1_values: list[float] = []
    for outcome in _E5_OUTCOMES:
        true_positive = confusion[outcome][outcome]
        expected_total = sum(confusion[outcome].values())
        predicted_total = sum(rows[outcome] for rows in confusion.values())
        recall = true_positive / expected_total if expected_total else 0.0
        precision = true_positive / predicted_total if predicted_total else 0.0
        recalls[outcome] = recall
        f1_values.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return {
        "macro_f1": sum(f1_values) / len(f1_values),
        "recall": recalls,
        "confusion": {name: dict(sorted(rows.items())) for name, rows in confusion.items()},
    }


def _partial_conservation(cases: Sequence[Mapping[str, Any]], indexed: Mapping[str, str]) -> tuple[float, list[str]]:
    checked = failed = 0
    failures: list[str] = []
    for case in cases:
        payload = case.get("input") or {}
        plan = payload.get("plan") or {}
        if (case.get("gold") or {}).get("decision") == "partial" or payload.get("partial_results"):
            checked += 1
            plan_amount = _decimal(plan.get("quantity"))
            results = payload.get("partial_results") or [payload.get("result") or {}]
            amounts = [_decimal(result.get("quantity")) for result in results]
            total = sum((item for item in amounts if item is not None), Decimal(0))
            expected_complete = bool(payload.get("partial_results"))
            valid = bool(
                plan_amount
                and all(item is not None and item > 0 for item in amounts)
                and (total == plan_amount if expected_complete else total < plan_amount)
            )
            if not valid or indexed.get(str(case["case_id"])) not in {"partial", "complete"}:
                failed += 1
                failures.append(str(case["case_id"]))
    return ((checked - failed) / checked if checked else 1.0), failures


def score_e5_predictions(
    cases: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    indexed = {str(row["case_id"]): str(row.get("decision")) for row in predictions}
    metrics = _classification_metrics(cases, predictions)
    error_closures = [
        str(case["case_id"])
        for case in cases
        if indexed.get(str(case["case_id"])) == "complete" and (case.get("gold") or {}).get("decision") != "complete"
    ]
    conservation, conservation_failures = _partial_conservation(cases, indexed)
    metrics.update(
        {
            "error_closures": len(error_closures),
            "error_closure_case_ids": error_closures,
            "partial_quantity_conservation": conservation,
            "partial_conservation_failure_case_ids": conservation_failures,
            "ambiguous_abstain_recall": metrics["recall"]["ambiguous"],
        }
    )
    failures = []
    if metrics["error_closures"]:
        failures.append("error_closures>0")
    if metrics["macro_f1"] < 0.95:
        failures.append("macro_f1<0.95")
    for outcome in ("complete", "cancel", "replace", "partial"):
        if metrics["recall"][outcome] < 0.90:
            failures.append(f"{outcome}_recall<0.90")
    if conservation != 1.0:
        failures.append("partial_conservation<1.0")
    if metrics["ambiguous_abstain_recall"] < 0.95:
        failures.append("ambiguous_abstain_recall<0.95")
    return {"metrics": metrics, "gate": {"passed": not failures, "failures": failures}}


def score_e6_predictions(
    cases: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    indexed = {str(row["case_id"]): row for row in predictions}
    target_predictions = target_correct = complete_targets = 0
    cross_target_supersede = 0
    missing_total = missing_uncertain = decision_exact = 0
    for case in cases:
        prediction = indexed.get(str(case["case_id"]), {})
        gold = case.get("gold") or {}
        left_expected = gold.get(
            "left_target_entity_id", (case.get("input") or {}).get("left", {}).get("canonical_target_entity_id")
        )
        right_expected = gold.get(
            "right_target_entity_id", (case.get("input") or {}).get("right", {}).get("canonical_target_entity_id")
        )
        predicted_targets = (prediction.get("left_target"), prediction.get("right_target"))
        expected_targets = (left_expected, right_expected)
        if all(predicted_targets):
            complete_targets += 1
        for predicted, expected in zip(predicted_targets, expected_targets):
            if predicted:
                target_predictions += 1
                target_correct += int(predicted == expected)
        if "cross_target" in case.get("risk_tags", []) and prediction.get("decision") == "snapshot_advance":
            cross_target_supersede += 1
        originally_missing = any(
            not side.get("canonical_target_entity_id") for side in (case.get("input") or {}).values()
        )
        if originally_missing:
            missing_total += 1
            missing_uncertain += int(prediction.get("decision") == "uncertain")
        decision_exact += int(prediction.get("decision") == gold.get("decision"))
    total = len(cases)
    metrics = {
        "exact_target_precision": target_correct / target_predictions if target_predictions else 0.0,
        "target_coverage": complete_targets / total if total else 0.0,
        "cross_target_supersede": cross_target_supersede,
        "missing_to_uncertain": missing_uncertain / missing_total if missing_total else 1.0,
        "series_decision_accuracy": decision_exact / total if total else 0.0,
    }
    failures = []
    if metrics["exact_target_precision"] != 1.0:
        failures.append("exact_target_precision<1.0")
    if metrics["target_coverage"] < 0.90:
        failures.append("target_coverage<0.90")
    if cross_target_supersede:
        failures.append("cross_target_supersede>0")
    if metrics["missing_to_uncertain"] != 1.0:
        failures.append("missing_to_uncertain<1.0")
    return {"metrics": metrics, "gate": {"passed": not failures, "failures": failures}}


def _e6_decision(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    left_target, right_target = left.get("canonical_target_entity_id"), right.get("canonical_target_entity_id")
    if not left_target or not right_target:
        return "uncertain"
    if left_target != right_target or left.get("price_axis") != right.get("price_axis"):
        return "distinct_series"
    if not left.get("snapshot_date") or not right.get("snapshot_date"):
        return "uncertain"
    return "snapshot_advance" if right["snapshot_date"] > left["snapshot_date"] else "uncertain"


def _e6_prediction(case: Mapping[str, Any], *, subject_mode: bool) -> dict[str, Any]:
    sides = case.get("input") or {}
    left, right = sides.get("left") or {}, sides.get("right") or {}
    if subject_mode:
        left_target = str(left.get("subject_entity_id") or "") or None
        right_target = str(right.get("subject_entity_id") or "") or None
        projected_left = {**left, "canonical_target_entity_id": left_target}
        projected_right = {**right, "canonical_target_entity_id": right_target}
    else:
        left_target = left.get("canonical_target_entity_id")
        right_target = right.get("canonical_target_entity_id")
        projected_left, projected_right = left, right
    return {
        "case_id": case["case_id"],
        "decision": _e6_decision(projected_left, projected_right),
        "left_target": left_target,
        "right_target": right_target,
    }


def _run_qwen(
    cases: Sequence[Mapping[str, Any]],
    runner: LocalQwenRunner,
    docket_factory: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    progress: Progress | None,
    reuse_results: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results: list[dict[str, Any]] = []
    calls = reused_calls = infrastructure_errors = model_errors = 0
    reusable = {str(row.get("case_id")): row for row in reuse_results}
    started = time.perf_counter()
    for index, case in enumerate(cases, 1):
        docket = docket_factory(case)
        previous = reusable.get(str(case.get("case_id")))
        if previous and previous.get("request_sha256") == request_hashes_for_docket(docket, runner):
            result = {**previous, "reused": True}
            reused_calls += int(result.get("call_count") or 0)
            results.append(result)
            continue
        try:
            result = runner.run_case(docket)
            calls += int(result.get("call_count") or 0)
            if not result.get("consistent"):
                model_errors += 1
        except Exception as error:  # fail closed and preserve the infrastructure/model distinction
            infrastructure_errors += 1
            result = {
                "case_id": str(case.get("case_id") or ""),
                "consistent": False,
                "decision": "infrastructure_failure",
                "call_count": len(runner.payload_snapshots),
                "failure_reason": type(error).__name__,
            }
            calls += len(runner.payload_snapshots)
        results.append(result)
        if progress and (index == 1 or index % 10 == 0 or index == len(cases)):
            progress(f"qwen {index}/{len(cases)} cases, {calls} calls")
    return results, {
        "calls": calls,
        "reused_calls": reused_calls,
        "effective_calls": calls + reused_calls,
        "elapsed_seconds": time.perf_counter() - started,
        "infrastructure_error_count": infrastructure_errors,
        "model_error_count": model_errors,
    }


def run_e5_v2(
    manifest: Mapping[str, Any],
    runner: LocalQwenRunner,
    *,
    progress: Progress | None = None,
    reuse_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    assessment = assess_e5_v2_manifest(manifest)
    if not assessment["ready"]:
        raise ValueError(f"E5 v2 preflight failed: {assessment['blockers']}")
    a_predictions = [{"case_id": case["case_id"], "decision": deterministic_e5_decision(case)} for case in cases]
    qwen_results, qwen = _run_qwen(
        cases,
        runner,
        qwen_e5_docket,
        progress=progress,
        reuse_results=(reuse_report or {}).get("qwen_case_results") or [],
    )
    b_predictions = [
        {
            "case_id": result["case_id"],
            "decision": result["decision"] if result.get("decision") in _E5_OUTCOMES else "ambiguous",
        }
        for result in qwen_results
    ]
    c_predictions = [{"case_id": case["case_id"], "decision": _wide_e5_decision(case)} for case in cases]
    arms = {
        "A": {"description": "strict_deterministic", **score_e5_predictions(cases, a_predictions)},
        "B": {"description": "strict_coordinates_plus_qwen", **score_e5_predictions(cases, b_predictions)},
        "C": {"description": "wide_semantic_risk_control", **score_e5_predictions(cases, c_predictions)},
    }
    selected = next((name for name in ("A", "B") if arms[name]["gate"]["passed"]), None)
    return {
        "schema_version": "v030-e5-report-v2",
        "status": "PASS" if selected else "SEALED_v2",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "assessment": assessment,
        "arms": arms,
        "selected_arm": selected,
        "qwen": qwen,
        "qwen_case_results": qwen_results,
        "enforce_recommendation": {
            "passed": bool(selected),
            "release_mode": "enforce" if selected else "audit",
            "configuration": 'plan.fulfillment_mode="enforce"' if selected else 'plan.fulfillment_mode="audit"',
        },
    }


def run_e6_v2(
    manifest: Mapping[str, Any],
    runner: LocalQwenRunner,
    references: Sequence[InstrumentReference],
    *,
    progress: Progress | None = None,
) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    assessment = assess_e6_v2_manifest(manifest)
    if not assessment["ready"]:
        raise ValueError(f"E6 v2 preflight failed: {assessment['blockers']}")
    a_predictions = [_e6_prediction(case, subject_mode=True) for case in cases]
    b_predictions = [_e6_prediction(case, subject_mode=False) for case in cases]
    missing_cases = [
        case
        for case in cases
        if any(not side.get("canonical_target_entity_id") for side in (case.get("input") or {}).values())
    ]

    def factory(case: Mapping[str, Any]) -> Mapping[str, Any]:
        return qwen_e6_docket(case, references)

    qwen_results, qwen = _run_qwen(missing_cases, runner, factory, progress=progress)
    qwen_by_id = {str(row["case_id"]): row for row in qwen_results}
    c_predictions = []
    for case, prediction in zip(cases, b_predictions):
        audit = qwen_by_id.get(str(case["case_id"]))
        if audit and audit.get("decision") == "resolved" and audit.get("consistent"):
            candidate = audit.get("winner_candidate_key")
            allowed = {item["candidate_key"] for item in factory(case)["candidates"]}
            if candidate in allowed:
                prediction = {**prediction, "left_target": candidate, "right_target": candidate}
        c_predictions.append(prediction)
    arms = {
        "A": {"description": "subject_heuristic", **score_e6_predictions(cases, a_predictions)},
        "B": {"description": "exact_code_typed_alias", **score_e6_predictions(cases, b_predictions)},
        "C": {"description": "B_plus_qwen_mention_audit", **score_e6_predictions(cases, c_predictions)},
    }
    passed = bool(arms["B"]["gate"]["passed"])
    return {
        "schema_version": "v030-e6-report-v2",
        "status": "PASS" if passed else "SEALED_v2",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "assessment": assessment,
        "arms": arms,
        "selected_arm": "B" if passed else None,
        "qwen": qwen,
        "qwen_case_results": qwen_results,
        "enforce_recommendation": {
            "passed": passed,
            "release_mode": "enforce" if passed else "observe",
            "configuration": 'price.target_mode="enforce"' if passed else 'price.target_mode="observe"',
        },
    }


def write_v2_report(output_dir: str | Path, report: Mapping[str, Any]) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    payload = copy.deepcopy(dict(report))
    payload["qwen"]["model_error_count"] = sum(
        row.get("failure_reason") == "candidate_order_disagreement" for row in payload.get("qwen_case_results") or []
    )
    report_path = root / "report_v2.json"
    summary_path = root / "summary_v2.md"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    arms = payload["arms"]
    summary_path.write_text(
        f"# {str(payload['schema_version']).split('-')[1].upper()} v2 replay\n\n"
        f"- Status: `{payload['status']}`\n"
        f"- Manifest: `{payload['manifest_sha256']}`\n"
        f"- Selected arm: `{payload['selected_arm']}`\n"
        f"- Qwen: `{json.dumps(payload['qwen'], sort_keys=True)}`\n"
        + "".join(
            f"- Arm {name}: gate=`{arm['gate']['passed']}`, metrics=`{json.dumps(arm['metrics'], sort_keys=True)}`\n"
            for name, arm in arms.items()
        )
        + f"- Enforce recommendation: `{json.dumps(payload['enforce_recommendation'], sort_keys=True)}`\n",
        encoding="utf-8",
    )
    artifacts = [report_path, summary_path]
    waiting_path = root / "waiting_qwen.json"
    if waiting_path.exists():
        artifacts.append(waiting_path)
    checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    (root / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="ascii"
    )
    return checksums


def load_v2_manifest(path: str | Path) -> dict[str, Any]:
    """Authenticate v2 through the shared manifest contract before replay."""

    return load_manifest(path)
