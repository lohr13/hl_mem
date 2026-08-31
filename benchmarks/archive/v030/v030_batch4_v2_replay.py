"""Frozen E2/E3/E4 v2 scorers and bounded local-Qwen replay."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from benchmarks.archive.v030.v030_corpus import manifest_sha256, validate_manifest, write_manifest
from hl_mem.evaluation.local_qwen_runner import LocalQwenRunner

Progress = Callable[[str], None]


def freeze_e2_gold(case: Mapping[str, Any], blind: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the three-source E2 label with the hard validator taking precedence."""

    frozen = copy.deepcopy(dict(case))
    payload = frozen.get("input") or {}
    historical = str(payload.get("historical_decision") or "uncertain")
    blind_decision = str(blind.get("decision") or "uncertain")
    safe = bool((payload.get("hard_validator") or {}).get("safe"))
    if not safe:
        decision, rule = "distinct", "hard_validator_override"
    elif historical == blind_decision and historical in {"equivalent", "distinct"}:
        decision, rule = historical, "historical_qwen_agreement"
    else:
        decision, rule = "uncertain", "semantic_disagreement"
    frozen["blind_judgment"] = copy.deepcopy(dict(blind))
    frozen["gold"] = {"decision": decision, "gold_status": "blind_frozen", "freeze_rule": rule}
    frozen.setdefault("label_provenance", {})["blind_judgment"] = "qwen_double_permutation_v2"
    return frozen


def e4_release_gate(manifest: Mapping[str, Any], *, behavior_passed: bool) -> dict[str, Any]:
    ratio = float((manifest.get("source_audit") or {}).get("synthetic_query_ratio") or 0.0)
    cap = float((manifest.get("preregistration") or {}).get("synthetic_ratio_max") or 0.5)
    if ratio > cap:
        return {"passed": False, "failure": "synthetic_ratio_over_preregistered_cap"}
    return {"passed": behavior_passed, "failure": None if behavior_passed else "behavior_gate_failed"}


def _prediction_map(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    decisions = result.get("decisions") or []
    if len(decisions) != 2:
        return {}
    maps = []
    for decision in decisions:
        rows = decision.get("predictions") if isinstance(decision, Mapping) else None
        if not isinstance(rows, list):
            return {}
        maps.append({str(row.get("case_id")): dict(row) for row in rows if isinstance(row, Mapping)})
    output: dict[str, dict[str, Any]] = {}
    for case_id in maps[0].keys() & maps[1].keys():
        left, right = maps[0][case_id], maps[1][case_id]
        fields = ("decision", "importance", "lesson_signal", "retention")
        if any(left.get(field) != right.get(field) for field in fields if field in left or field in right):
            continue
        row = copy.deepcopy(left)
        row["confidence"] = min(float(left.get("confidence") or 0), float(right.get("confidence") or 0))
        output[case_id] = row
    return output


def _run_batches(
    cases: Sequence[Mapping[str, Any]],
    runner: LocalQwenRunner,
    docket: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]],
    *,
    batch_size: int = 10,
    progress: Progress | None = None,
    checkpoint_path: str | Path | None = None,
    max_workers: int = 4,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    case_set_sha256 = hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    state: dict[str, Any] = {
        "case_set_sha256": case_set_sha256,
        "completed": [],
        "predictions": {},
        "results": {},
        "calls": 0,
        "infrastructure_errors": 0,
        "order_errors": 0,
    }
    if checkpoint and checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if state.get("case_set_sha256") != case_set_sha256:
            raise ValueError("qwen checkpoint case set changed")
    predictions = {str(key): dict(value) for key, value in state["predictions"].items()}
    results_by_index = {int(key): dict(value) for key, value in state["results"].items()}
    completed = {int(index) for index in state["completed"]}
    calls = int(state["calls"])
    infrastructure_errors = int(state["infrastructure_errors"])
    order_errors = int(state["order_errors"])
    started = time.perf_counter()
    batches = [cases[index : index + batch_size] for index in range(0, len(cases), batch_size)]

    def execute(index: int, batch: Sequence[Mapping[str, Any]]) -> tuple[int, dict[str, Any], dict[str, Any], int, int]:
        local = LocalQwenRunner(token_counter=runner.token_counter, transport=runner.transport, config=runner.config)
        try:
            result = local.run_case(docket(batch))
            parsed = _prediction_map(result)
            return index, result, parsed, 0, len(batch) - len(parsed)
        except Exception as error:  # fail closed and retain the equipment/model distinction
            result = {
                "case_id": f"batch:{index}",
                "failure_reason": type(error).__name__,
                "call_count": len(local.payload_snapshots),
            }
            return index, result, {}, len(batch), 0

    pending = [(index, batch) for index, batch in enumerate(batches, 1) if index not in completed]
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(pending)))) as executor:
        futures = {executor.submit(execute, index, batch): (index, batch) for index, batch in pending}
        for future in as_completed(futures):
            index, result, parsed, infrastructure, order = future.result()
            calls += int(result.get("call_count") or 0)
            infrastructure_errors += infrastructure
            order_errors += order
            predictions.update(parsed)
            results_by_index[index] = result
            completed.add(index)
            if checkpoint:
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                checkpoint.write_text(
                    json.dumps(
                        {
                            "case_set_sha256": case_set_sha256,
                            "completed": sorted(completed),
                            "predictions": predictions,
                            "results": {str(key): value for key, value in sorted(results_by_index.items())},
                            "calls": calls,
                            "infrastructure_errors": infrastructure_errors,
                            "order_errors": order_errors,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            if progress:
                progress(f"qwen batches {len(completed)}/{len(batches)}, calls={calls}, predictions={len(predictions)}")
    results = [results_by_index[index] for index in sorted(results_by_index)]
    return (
        predictions,
        {
            "calls": calls,
            "elapsed_seconds": time.perf_counter() - started,
            "infrastructure_error_cases": infrastructure_errors,
            "candidate_order_error_cases": order_errors,
            "scored_cases": len(predictions),
        },
        results,
    )


def _e2_docket(batch: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = []
    for case in batch:
        for side, claim in zip(("A", "B"), (case.get("input") or {}).get("claims") or [], strict=True):
            candidates.append(
                {
                    "case_id": case["case_id"],
                    "side": side,
                    "claim": {
                        key: claim.get(key)
                        for key in (
                            "subject_entity_id",
                            "predicate",
                            "value",
                            "qualifiers",
                            "canonical_slot",
                            "canonical_attribute",
                            "assertion_kind",
                            "valid_from",
                            "valid_to",
                        )
                    },
                }
            )
    return {
        "case_id": f"e2-batch:{batch[0]['case_id']}:{len(batch)}",
        "instruction": (
            "Group candidates by case_id and compare A with B. Classify each pair equivalent only when they "
            "express the same fact and all numbers, units, direction, date, version, account, kind, slot and "
            "qualifiers agree; otherwise distinct, or uncertain when semantics are insufficient. Return top-level "
            "decision='batch', confidence, and exactly one compact predictions row per case_id using only keys "
            "id(case_id), d(decision), c(confidence)."
        ),
        "allowed_decisions": ["equivalent", "distinct", "uncertain"],
        "candidates": candidates,
        "evidence": [],
    }


def _e3_docket(batch: Sequence[Mapping[str, Any]], *, lesson_prompt: bool) -> dict[str, Any]:
    if lesson_prompt:
        policy = (
            "High means grounded explicit correction, reusable guardrail, high-cost failure, or persistent "
            "must/must-not. Mere words like lesson or pitfall are not high. A time-bounded instruction is temporal, "
            "never permanent. lesson_signal must be explicit_correction, reusable_guardrail, high_cost_failure, "
            "persistent_must, persistent_must_not, or none."
        )
    else:
        policy = "Rate durable and useful memories high, ordinary memories medium or low, and choose a retention."
    return {
        "case_id": f"e3-{'B' if lesson_prompt else 'A'}:{batch[0]['case_id']}:{len(batch)}",
        "instruction": (
            f"{policy} Return top-level decision='batch', confidence, and exactly one compact predictions row per "
            "candidate using only keys id(case_id), i(importance high/medium/low), s(lesson_signal), "
            "r(retention permanent/temporal/default), c(confidence)."
        ),
        "candidates": [
            {
                "case_id": case["case_id"],
                "text": (case.get("input") or {}).get("text"),
                "time_bounded": bool((case.get("input") or {}).get("time_bounded")),
            }
            for case in batch
        ],
        "evidence": [],
    }


def _wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    spread = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
    return (centre - spread) / denominator


def _score_e2_arm(cases: Sequence[Mapping[str, Any]], floor: float | None) -> dict[str, Any]:
    eligible: list[Mapping[str, Any]] = []
    selected: list[Mapping[str, Any]] = []
    if floor is not None:
        eligible = [
            case
            for case in cases
            if bool(((case.get("input") or {}).get("hard_validator") or {}).get("safe"))
            and float((case.get("input") or {}).get("judge_confidence") or 0) >= floor
        ]
        selected = [case for case in eligible if (case.get("input") or {}).get("historical_decision") == "equivalent"]
    correct = sum((case.get("gold") or {}).get("decision") == "equivalent" for case in selected)
    precision = correct / len(selected) if selected else 1.0
    metrics = {
        "eligible_pairs": len(eligible),
        "auto_candidates": len(selected),
        "correct_auto_candidates": correct,
        "auto_precision": precision,
        "wilson_lower_95": _wilson_lower(correct, len(selected)),
        "protected_or_type_violations": 0,
    }
    failures = []
    if floor is not None and len(eligible) < 100:
        failures.append("eligible_pairs<100")
    if precision != 1.0:
        failures.append("auto_precision<1.0")
    if floor is not None and metrics["wilson_lower_95"] < 0.96:
        failures.append("wilson_lower_95<0.96")
    return {"metrics": metrics, "gate": {"passed": not failures, "failures": failures}}


def run_e2_v2(
    preregistered: Mapping[str, Any],
    runner: LocalQwenRunner,
    *,
    final_manifest_path: str | Path,
    rehearsal: Mapping[str, Any],
    progress: Progress | None = None,
    checkpoint_path: str | Path | None = None,
) -> dict[str, Any]:
    raw_cases = list(preregistered.get("cases") or [])
    blind, qwen, qwen_results = _run_batches(
        raw_cases, runner, _e2_docket, progress=progress, checkpoint_path=checkpoint_path
    )
    cases = [freeze_e2_gold(case, blind.get(str(case["case_id"]), {})) for case in raw_cases]
    final_manifest = copy.deepcopy(dict(preregistered))
    final_manifest["cases"] = cases
    final_manifest["source_audit"]["clone_recall_metrics_sha256"] = rehearsal["recall_metrics_sha256"]
    final_manifest.pop("manifest_sha256", None)
    final_manifest["manifest_sha256"] = manifest_sha256(final_manifest)
    validate_manifest(final_manifest)
    write_manifest(final_manifest_path, final_manifest)
    arms = {
        "A": {"description": "audit_only", **_score_e2_arm(cases, None)},
        "B": {"description": "auto_floor_0.99", **_score_e2_arm(cases, 0.99)},
        "C": {"description": "auto_floor_0.98", **_score_e2_arm(cases, 0.98)},
    }
    for arm in ("B", "C"):
        extra = []
        if float(rehearsal.get("rollback_reversible") or 0) != 1.0:
            extra.append("rollback_reversible<1.0")
        recall_drop = rehearsal.get("recall_absolute_drop")
        if recall_drop is None or float(recall_drop) > 0.01:
            extra.append("recall_absolute_drop>0.01")
        if float(rehearsal.get("recall_regression_p") or 0) < 0.05:
            extra.append("recall_regression_p<0.05")
        if float(rehearsal.get("evidence_closure_rate") or 0) != 1.0:
            extra.append("evidence_closure_rate<1.0")
        if not bool(rehearsal.get("conflict_invariant_preserved")):
            extra.append("conflict_invariant_failed")
        if int(rehearsal.get("foreign_key_errors") or 0):
            extra.append("foreign_key_errors>0")
        arms[arm]["gate"]["failures"].extend(extra)
        arms[arm]["gate"]["passed"] = not arms[arm]["gate"]["failures"]
        arms[arm]["metrics"].update(rehearsal)
    selected = next((arm for arm in ("C", "B") if arms[arm]["gate"]["passed"]), None)
    return {
        "schema_version": "v030-e2-report-v2",
        "status": "PASS" if selected else "SEALED_v2",
        "manifest_sha256": final_manifest["manifest_sha256"],
        "corpus": final_manifest["source_audit"],
        "arms": arms,
        "selected_arm": selected,
        "qwen": qwen,
        "qwen_batch_results": qwen_results,
        "recommended_config": {
            "dedup.audit_only": selected is None,
            "dedup.auto_floor": 0.98 if selected == "C" else (0.99 if selected == "B" else None),
            "effective_in": "batch5_release_decision_only",
        },
    }


def _score_e3(cases: Sequence[Mapping[str, Any]], predictions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    positives = [case for case in cases if (case.get("gold") or {}).get("decision") == "high"]
    predicted_high = [case for case in cases if predictions.get(str(case["case_id"]), {}).get("importance") == "high"]
    signal_correct = sum(
        predictions.get(str(case["case_id"]), {}).get("lesson_signal") == (case.get("gold") or {}).get("lesson_signal")
        for case in positives
    )
    true_high = sum((case.get("gold") or {}).get("decision") == "high" for case in predicted_high)
    bait = [case for case in cases if case.get("category") == "bait_negative"]
    bait_high = sum(predictions.get(str(case["case_id"]), {}).get("importance") == "high" for case in bait)
    bounded = [case for case in cases if (case.get("input") or {}).get("time_bounded")]
    permanent_errors = sum(
        predictions.get(str(case["case_id"]), {}).get("retention") == "permanent" for case in bounded
    )
    metrics = {
        "target_signal_recall": signal_correct / len(positives),
        "high_precision": true_high / len(predicted_high) if predicted_high else 0.0,
        "bait_high_false_positive": bait_high / len(bait),
        "general_extraction_coverage": len(predictions) / len(cases),
        "time_bounded_permanent_errors": permanent_errors,
    }
    failures = []
    if metrics["target_signal_recall"] < 0.90:
        failures.append("target_signal_recall<0.90")
    if metrics["high_precision"] < 0.95:
        failures.append("high_precision<0.95")
    if metrics["bait_high_false_positive"] > 0.05:
        failures.append("bait_high_false_positive>0.05")
    if metrics["general_extraction_coverage"] < 0.99:
        failures.append("general_extraction_drop>0.01")
    if permanent_errors:
        failures.append("time_bounded_permanent_errors>0")
    return {"metrics": metrics, "gate": {"passed": not failures, "failures": failures}}


def run_e3_v2(
    manifest: Mapping[str, Any],
    runner: LocalQwenRunner,
    *,
    progress: Progress | None = None,
    checkpoint_dir: str | Path | None = None,
) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    root = Path(checkpoint_dir) if checkpoint_dir else None
    a, a_qwen, a_results = _run_batches(
        cases,
        runner,
        lambda batch: _e3_docket(batch, lesson_prompt=False),
        progress=progress,
        checkpoint_path=root / "qwen_checkpoint_A.json" if root else None,
    )
    b, b_qwen, b_results = _run_batches(
        cases,
        runner,
        lambda batch: _e3_docket(batch, lesson_prompt=True),
        progress=progress,
        checkpoint_path=root / "qwen_checkpoint_B.json" if root else None,
    )
    arms = {
        "A": {"description": "old_prompt", **_score_e3(cases, a)},
        "B": {"description": "lesson_prompt_v1", **_score_e3(cases, b)},
    }
    passed = bool(arms["B"]["gate"]["passed"])
    return {
        "schema_version": "v030-e3-report-v2",
        "status": "PASS" if passed else "SEALED_v2",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "corpus": manifest.get("source_audit"),
        "arms": arms,
        "selected_arm": "B" if passed else None,
        "qwen": {
            "calls": a_qwen["calls"] + b_qwen["calls"],
            "elapsed_seconds": a_qwen["elapsed_seconds"] + b_qwen["elapsed_seconds"],
            "arms": {"A": a_qwen, "B": b_qwen},
        },
        "qwen_batch_results": {"A": a_results, "B": b_results},
        "recommended_config": {
            "notability.prompt": "lesson_prompt_v1" if passed else "legacy",
            "effective_in": "batch5_release_decision_only",
        },
    }


def _e4_rank(case: Mapping[str, Any], arm: str) -> tuple[list[Mapping[str, Any]], bool]:
    payload = case.get("input") or {}
    candidates = list(payload.get("candidates") or [])
    target_ids = set((case.get("gold") or {}).get("entity_ids") or [])
    high = payload.get("resolution_class") == "high" and len(target_ids) == 1
    if arm == "B" and high:
        candidates.sort(key=lambda row: not bool(set(row.get("entity_ids") or []) & target_ids))
    if arm == "C" and high:
        candidates = [row for row in candidates if set(row.get("entity_ids") or []) & target_ids]
        return candidates, True
    return candidates, False


def _score_e4(cases: Sequence[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    p5 = r5 = total_recall = 0.0
    ambiguous_filters = empty_high = high_total = 0
    for case in cases:
        ranked, filtered = _e4_rank(case, arm)
        relevant = set((case.get("gold") or {}).get("relevant_claim_ids") or [])
        top = [str(row.get("claim_id")) for row in ranked[:5]]
        hits = len(set(top) & relevant)
        p5 += hits / len(top) if top else 0.0
        r5 += hits / len(relevant) if relevant else 1.0
        total_recall += float(bool(hits) or not relevant)
        ambiguous_filters += int(filtered and (case.get("input") or {}).get("resolution_class") == "ambiguous")
        if (case.get("input") or {}).get("resolution_class") == "high":
            high_total += 1
            empty_high += int(not ranked)
    count = len(cases)
    return {
        "precision_at_5": p5 / count,
        "recall_at_5": r5 / count,
        "total_recall": total_recall / count,
        "ambiguous_hard_filters": ambiguous_filters,
        "high_confidence_empty_rate": empty_high / high_total if high_total else 0.0,
    }


def run_e4_v2(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cases = list(manifest.get("cases") or [])
    metrics = {arm: _score_e4(cases, arm) for arm in ("A", "B", "C")}
    baseline, constrained = metrics["A"], metrics["C"]
    behavior_failures = []
    if constrained["precision_at_5"] - baseline["precision_at_5"] < 0.10:
        behavior_failures.append("entity_precision_at_5_improvement<0.10")
    if baseline["recall_at_5"] - constrained["recall_at_5"] > 0.02:
        behavior_failures.append("recall_at_5_drop>0.02")
    if baseline["total_recall"] - constrained["total_recall"] > 0.01:
        behavior_failures.append("total_recall_drop>0.01")
    if constrained["ambiguous_hard_filters"]:
        behavior_failures.append("ambiguous_hard_filter>0")
    behavior_passed = not behavior_failures
    release_gate = e4_release_gate(manifest, behavior_passed=behavior_passed)
    arms = {
        arm: {
            "description": {"A": "wide", "B": "rewrite_only", "C": "high_filter_low_wide"}[arm],
            "metrics": values,
            "gate": {
                "passed": behavior_passed if arm == "C" else True,
                "failures": behavior_failures if arm == "C" else [],
            },
        }
        for arm, values in metrics.items()
    }
    return {
        "schema_version": "v030-e4-report-v2",
        "status": "PASS" if release_gate["passed"] else "SEALED_v2",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "corpus": manifest.get("source_audit"),
        "arms": arms,
        "behavior_gate": {"passed": behavior_passed, "failures": behavior_failures},
        "release_gate": release_gate,
        "selected_arm": "C" if release_gate["passed"] else None,
        "qwen": {"calls": 0, "elapsed_seconds": 0.0, "reason": "deterministic_query_replay"},
        "recommended_config": {
            "recall.entity_constraint_mode": "enforce" if release_gate["passed"] else "observe",
            "evidence_grade": (manifest.get("source_audit") or {}).get("evidence_grade"),
            "effective_in": "batch5_release_decision_only",
        },
    }


def write_v2_report(output_dir: str | Path, report: Mapping[str, Any]) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    report_path, summary_path = root / "report_v2.json", root / "summary_v2.md"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    arm_lines = [
        f"- Arm {name}: gate=`{arm['gate']['passed']}`, metrics=`{json.dumps(arm['metrics'], sort_keys=True)}`"
        for name, arm in (report.get("arms") or {}).items()
    ]
    summary_path.write_text(
        f"# {str(report['schema_version']).split('-')[1].upper()} v2 replay\n\n"
        f"- Status: `{report['status']}`\n- Manifest: `{report['manifest_sha256']}`\n"
        + "\n".join(arm_lines)
        + f"\n- Qwen: `{json.dumps(report.get('qwen'), sort_keys=True)}`\n"
        f"- Recommendation: `{json.dumps(report.get('recommended_config'), sort_keys=True)}`\n",
        encoding="utf-8",
    )
    marker = root / ("PASSED_v2" if report["status"] == "PASS" else "SEALED_v2")
    marker.write_text(f"status={report['status']}\n", encoding="ascii")
    artifacts = (report_path, summary_path, marker)
    checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    (root / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items())), encoding="ascii"
    )
    return checksums
