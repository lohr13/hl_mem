from __future__ import annotations

import json
from pathlib import Path

from benchmarks.archive.v030.v030_plan_price import assess_e5_manifest, assess_e6_manifest, write_sealed_report


def _manifest(experiment: str, cases: list[dict]) -> dict:
    return {
        "experiment": experiment,
        "manifest_sha256": "a" * 64,
        "source_snapshots": [{"source_id": "fixture", "sha256": "b" * 64, "reconstructable": True}],
        "cases": cases,
    }


def test_e5_preflight_rejects_gold_collision_and_incomplete_coordinates() -> None:
    core = {
        "plan": {"target": "instrument:x", "quantity": "100", "unit": "?"},
        "result": {"target": "instrument:x", "quantity": "100", "unit": "?"},
    }
    manifest = _manifest(
        "E5",
        [
            {"case_id": "complete", "input": core, "gold": {"decision": "complete"}},
            {"case_id": "cancel", "input": core, "gold": {"decision": "cancel"}},
        ],
    )

    assessment = assess_e5_manifest(manifest)

    assert assessment["ready"] is False
    assert assessment["counts"]["ambiguous_core_input_groups"] == 1
    assert assessment["counts"]["unknown_unit_cases"] == 2
    assert assessment["counts"]["missing_result_phase_cases"] == 2


def test_e6_preflight_rejects_unfrozen_gold_and_missing_series_pair() -> None:
    manifest = _manifest(
        "E6",
        [
            {
                "case_id": "pending",
                "instrument_id": "legacy:x",
                "input": {"claim": {"id": "x"}},
                "gold": {"decision": "pending_manual_freeze"},
            },
            {
                "case_id": "synthetic",
                "instrument_id": "instrument:y",
                "input": {"axis": "spot", "value": "10"},
                "gold": {"decision": "distinct_series"},
            },
        ],
    )

    assessment = assess_e6_manifest(manifest)

    assert assessment["ready"] is False
    assert assessment["counts"]["pending_gold_cases"] == 1
    assert assessment["counts"]["missing_series_pair_cases"] == 2


def test_sealed_report_records_unavailable_metrics_without_model_error(tmp_path: Path) -> None:
    manifest = _manifest(
        "E6",
        [
            {
                "case_id": "pending",
                "input": {},
                "gold": {"decision": "pending_manual_freeze"},
            }
        ],
    )
    assessment = assess_e6_manifest(manifest)

    report = write_sealed_report(tmp_path, manifest, assessment)

    assert report["status"] == "SEALED_FAILED"
    assert report["qwen"]["model_error_count"] == 0
    assert report["qwen"]["calls"] == 0
    assert report["gate"]["metrics"] == "not_computable"
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == report
