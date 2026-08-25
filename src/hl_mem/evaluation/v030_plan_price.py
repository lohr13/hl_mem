"""Frozen-corpus readiness gates for the E5 plan and E6 price experiments."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unreconstructable_count(manifest: Mapping[str, Any]) -> int:
    return sum(
        1
        for snapshot in manifest.get("source_snapshots") or []
        if isinstance(snapshot, Mapping) and not snapshot.get("reconstructable")
    )


def assess_e5_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Reject gold-leaking or coordinate-incomplete plan corpora before any qwen call."""

    if manifest.get("experiment") != "E5":
        raise ValueError("E5 assessment requires an E5 manifest")
    cases = list(manifest.get("cases") or [])
    core_labels: dict[str, set[str]] = defaultdict(set)
    unknown_unit = 0
    missing_phase = 0
    missing_action = 0
    risk_contradictions = 0
    for case in cases:
        input_payload = case.get("input") or {}
        plan = input_payload.get("plan") or {}
        result = input_payload.get("result") or {}
        core_labels[_canonical({"plan": plan, "result": result})].add(
            str((case.get("gold") or {}).get("decision") or "")
        )
        if plan.get("unit") in {None, "?"} or result.get("unit") in {None, "?"}:
            unknown_unit += 1
        if not result.get("assertion_phase"):
            missing_phase += 1
        if not plan.get("action_family") or not result.get("action_family"):
            missing_action += 1
        tags = set(case.get("risk_tags") or [])
        gold = str((case.get("gold") or {}).get("decision") or "")
        if gold == "complete" and tags & {"negation", "cross_account"}:
            risk_contradictions += 1
        if "gold_10500" in tags and str(input_payload.get("actual_quantity")) != str(plan.get("quantity")):
            risk_contradictions += 1
    counts = {
        "cases": len(cases),
        "ambiguous_core_input_groups": sum(len(labels) > 1 for labels in core_labels.values()),
        "unknown_unit_cases": unknown_unit,
        "missing_result_phase_cases": missing_phase,
        "missing_action_coordinate_cases": missing_action,
        "risk_label_contradictions": risk_contradictions,
        "unreconstructable_source_snapshots": _unreconstructable_count(manifest),
    }
    blockers = [name for name, value in counts.items() if name != "cases" and value]
    return {"experiment": "E5", "ready": not blockers, "counts": counts, "blockers": blockers}


def _has_series_pair(input_payload: Mapping[str, Any]) -> bool:
    pair = None
    if isinstance(input_payload.get("left"), Mapping) and isinstance(input_payload.get("right"), Mapping):
        pair = (input_payload["left"], input_payload["right"])
    elif isinstance(input_payload.get("candidate"), Mapping) and isinstance(input_payload.get("current"), Mapping):
        pair = (input_payload["candidate"], input_payload["current"])
    if pair is None:
        return False
    required = {"price_axis", "canonical_target_entity_id", "snapshot_date"}
    return all(required <= set(item) for item in pair)


def assess_e6_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Require frozen gold and both sides of the three-dimensional price coordinate."""

    if manifest.get("experiment") != "E6":
        raise ValueError("E6 assessment requires an E6 manifest")
    cases = list(manifest.get("cases") or [])
    pending = sum((case.get("gold") or {}).get("decision") == "pending_manual_freeze" for case in cases)
    missing_pairs = sum(not _has_series_pair(case.get("input") or {}) for case in cases)
    instruments = {str(case.get("instrument_id") or "") for case in cases}
    instruments.discard("")
    counts = {
        "cases": len(cases),
        "instruments": len(instruments),
        "pending_gold_cases": pending,
        "missing_series_pair_cases": missing_pairs,
        "unreconstructable_source_snapshots": _unreconstructable_count(manifest),
    }
    blockers = [
        name
        for name in ("pending_gold_cases", "missing_series_pair_cases", "unreconstructable_source_snapshots")
        if counts[name]
    ]
    return {"experiment": "E6", "ready": not blockers, "counts": counts, "blockers": blockers}


def write_sealed_report(
    output_dir: str | Path,
    manifest: Mapping[str, Any],
    assessment: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal a corpus failure without counting it as a model error or fabricating metrics."""

    experiment = str(assessment["experiment"])
    release_mode = "audit" if experiment == "E5" else "observe"
    report = {
        "schema_version": f"v030-{experiment.lower()}-sealed-v1",
        "status": "SEALED_FAILED",
        "failure_class": "corpus_contract_failure",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "assessment": dict(assessment),
        "execution_history": ["authenticated_manifest", "corpus_preflight", "SEALED_FAILED"],
        "qwen": {
            "state": "not_called_preflight_failed",
            "calls": 0,
            "model_error_count": 0,
            "infrastructure_error_count": 0,
        },
        "gate": {"passed": False, "metrics": "not_computable"},
        "enforce_recommendation": {"passed": False, "release_mode": release_mode},
    }
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.json"
    summary_path = root / "summary.md"
    report_path.write_text(_pretty_json(report), encoding="utf-8")
    summary_path.write_text(
        f"# {experiment} SEALED_FAILED\n\n"
        "Frozen corpus preflight failed before any model call. This is not a qwen error.\n\n"
        f"- Blockers: `{_canonical(assessment['blockers'])}`\n"
        f"- Counts: `{_canonical(assessment['counts'])}`\n"
        f"- Release mode remains: `{release_mode}`\n",
        encoding="utf-8",
    )
    checksums = [f"{_sha256(path)}  {path.name}" for path in (report_path, summary_path)]
    (root / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="ascii")
    return report


def _pretty_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
