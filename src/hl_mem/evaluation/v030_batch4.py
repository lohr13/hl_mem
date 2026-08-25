"""Readiness gates and sealed reports for the frozen E2/E3/E4 corpora."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, cast

_PLACEHOLDER_RE = re.compile(r"^\?+\s+[a-z_]+\s+\d+$", re.IGNORECASE)


def _cases(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [case for case in manifest.get("cases") or [] if isinstance(case, Mapping)]


def _assess_e2(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cases = _cases(manifest)
    claims = [claim for case in cases for claim in (case.get("input") or {}).get("claims") or []]
    missing_typed = sum(
        not (claim.get("subject_canonical_entity_id") or claim.get("canonical_target_entity_id")) for claim in claims
    )
    unfrozen = sum(
        str((case.get("gold") or {}).get("gold_status") or "").startswith("historical_decision") for case in cases
    )
    missing_blind = sum(not case.get("blind_judgment") for case in cases)
    source_audit = manifest.get("source_audit") or {}
    counts = {
        "cases": len(cases),
        "eligible_pairs": sum(
            all(
                claim.get("subject_canonical_entity_id") or claim.get("canonical_target_entity_id")
                for claim in (case.get("input") or {}).get("claims") or []
            )
            for case in cases
        ),
        "missing_typed_coordinate_claims": missing_typed,
        "unfrozen_gold_cases": unfrozen,
        "missing_blind_judgment_cases": missing_blind,
        "missing_clone_recall_metrics": int(not source_audit.get("clone_recall_metrics_sha256")),
        "below_minimum_cases": max(0, 406 - len(cases)),
    }
    blockers = [name for name, value in counts.items() if name not in {"cases", "eligible_pairs"} and value]
    return {"experiment": "E2", "ready": not blockers, "counts": counts, "blockers": blockers}


def _assess_e3(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cases = _cases(manifest)
    audit = manifest.get("source_audit") or {}
    counts = {
        "cases": len(cases),
        "placeholder_text_cases": sum(
            bool(_PLACEHOLDER_RE.fullmatch(str((case.get("input") or {}).get("text") or ""))) for case in cases
        ),
        "existing_extraction_set_pending": int(audit.get("existing_extraction_set") == "PENDING_LINK"),
        "production_examples": int(audit.get("production_examples") or 0),
        "below_minimum_cases": max(0, 240 - len(cases)),
    }
    blockers = [
        name
        for name in ("placeholder_text_cases", "existing_extraction_set_pending", "below_minimum_cases")
        if counts[name]
    ]
    if counts["production_examples"] == 0:
        blockers.append("production_examples")
    return {"experiment": "E3", "ready": not blockers, "counts": counts, "blockers": blockers}


def _assess_e4(manifest: Mapping[str, Any]) -> dict[str, Any]:
    cases = _cases(manifest)
    audit = manifest.get("source_audit") or {}
    counts = {
        "cases": len(cases),
        "placeholder_query_cases": sum(
            bool(_PLACEHOLDER_RE.fullmatch(str((case.get("input") or {}).get("query") or ""))) for case in cases
        ),
        "pending_gold_cases": sum(
            (case.get("gold") or {}).get("gold_status") == "pending_manual_freeze" for case in cases
        ),
        "production_query_log_missing": int(audit.get("production_query_log") == "NOT_PROVIDED"),
        "below_minimum_cases": max(0, 240 - len(cases)),
    }
    blockers = [name for name, value in counts.items() if name != "cases" and value]
    return {"experiment": "E4", "ready": not blockers, "counts": counts, "blockers": blockers}


def assess_batch4_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    experiment = str(manifest.get("experiment") or "")
    assessors = {"E2": _assess_e2, "E3": _assess_e3, "E4": _assess_e4}
    if experiment not in assessors:
        raise ValueError("batch4 assessment accepts only E2, E3, or E4")
    return assessors[experiment](manifest)


def _arm_rows(experiment: str, cases: int) -> dict[str, dict[str, Any]]:
    definitions = {
        "E2": {"A": "audit-only", "B": "auto_floor=0.99", "C": "auto_floor=0.98"},
        "E3": {"A": "old_prompt", "B": "lesson_prompt_v1"},
        "E4": {"A": "wide", "B": "rewrite_only", "C": "high_filter_low_wide"},
    }
    return {
        arm: {
            "policy": policy,
            "input_cases": cases,
            "scored_cases": 0,
            "metrics": None,
            "gate_passed": False,
            "not_computable_reason": "corpus_contract_failure",
        }
        for arm, policy in definitions[experiment].items()
    }


def _recommendation(experiment: str) -> dict[str, Any]:
    recommendations: dict[str, dict[str, Any]] = {
        "E2": {"dedup.audit_only": True, "auto_floor": None},
        "E3": {"extraction.lesson_signal_mode": "observe"},
        "E4": {"recall.entity_constraint_mode": "observe"},
    }
    return recommendations[experiment]


def write_batch4_report(
    output_dir: str | Path,
    manifest: Mapping[str, Any],
    assessment: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal an unscorable corpus before behavior or model execution."""

    if assessment.get("ready"):
        raise ValueError("ready corpus must be passed to its replay runner")
    experiment = str(assessment["experiment"])
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    history = ["authenticated_manifest"]
    waiting_path = root / "waiting_qwen.json"
    if experiment == "E3":
        waiting = {
            "schema_version": "v030-e3-waiting-qwen-v1",
            "status": "WAITING_QWEN",
            "service_owner": "Hermes",
            "manifest_sha256": manifest.get("manifest_sha256"),
        }
        waiting_path.write_text(_pretty(waiting), encoding="utf-8")
        history.append("WAITING_QWEN")
    history.extend(("corpus_preflight", "SEALED_FAILED"))
    arms = _arm_rows(experiment, int(cast(Mapping[str, Any], assessment["counts"])["cases"]))
    report: dict[str, Any] = {
        "schema_version": f"v030-{experiment.lower()}-sealed-v1",
        "status": "SEALED_FAILED",
        "failure_class": "corpus_contract_failure",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "assessment": dict(assessment),
        "category_counts": dict(sorted(Counter(str(case.get("category")) for case in _cases(manifest)).items())),
        "arms": arms,
        "execution_history": history,
        "qwen": {"state": "not_called_preflight_failed", "calls": 0, "model_errors": 0},
        "gate": {"passed": False, "metrics": None},
        "recommended_config": _recommendation(experiment),
    }
    report_path = root / "report.json"
    summary_path = root / "summary.md"
    report_path.write_text(_pretty(report), encoding="utf-8")
    arm_lines = [f"| {name} | {arm['policy']} | {arm['input_cases']} | 0 | N/A |" for name, arm in arms.items()]
    summary_path.write_text(
        f"# {experiment} SEALED_FAILED\n\n"
        "Corpus preflight failed before scoring or model calls; N/A is not a zero score.\n\n"
        "| Arm | Policy | Inputs | Scored | Metrics |\n|---|---|---:|---:|---|\n"
        + "\n".join(arm_lines)
        + f"\n\n- Blockers: `{json.dumps(assessment['blockers'], ensure_ascii=False)}`"
        + f"\n- Recommended config: `{json.dumps(report['recommended_config'], ensure_ascii=False)}`\n",
        encoding="utf-8",
    )
    seal_path = root / "SEALED_FAILED"
    seal_path.write_text(
        f"report_sha256={_sha256(report_path)}\nsummary_sha256={_sha256(summary_path)}\n",
        encoding="ascii",
    )
    outputs = [report_path, summary_path, seal_path]
    if waiting_path.exists():
        outputs.append(waiting_path)
    (root / "SHA256SUMS").write_text(
        "\n".join(f"{_sha256(path)}  {path.name}" for path in outputs) + "\n", encoding="ascii"
    )
    return report


def _pretty(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
