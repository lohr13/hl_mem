from __future__ import annotations

import pytest

from hl_mem.evaluation.v030_batch4 import assess_batch4_manifest, write_batch4_report


def _manifest(experiment: str) -> dict[str, object]:
    inputs = {
        "E2": {
            "claims": [
                {"id": "left", "canonical_slot": "config.x"},
                {"id": "right", "canonical_slot": "config.x"},
            ]
        },
        "E3": {"text": "???? correction 000"},
        "E4": {"query": "???? unique_alias 000"},
    }
    gold = {
        "E2": {"gold_status": "historical_decision_not_natural_gold"},
        "E3": {"gold_status": "rule_frozen_synthetic"},
        "E4": {"gold_status": "pending_manual_freeze"},
    }
    audits = {
        "E2": {},
        "E3": {"existing_extraction_set": "PENDING_LINK", "production_examples": 0},
        "E4": {"production_query_log": "NOT_PROVIDED"},
    }
    return {
        "experiment": experiment,
        "manifest_sha256": experiment.lower() * 32,
        "cases": [{"case_id": "case-1", "input": inputs[experiment], "gold": gold[experiment]}],
        "source_audit": audits[experiment],
    }


@pytest.mark.parametrize(
    ("experiment", "blocker"),
    [
        ("E2", "missing_typed_coordinate_claims"),
        ("E3", "placeholder_text_cases"),
        ("E4", "pending_gold_cases"),
    ],
)
def test_batch4_preflight_seals_unscorable_corpora(tmp_path, experiment: str, blocker: str) -> None:
    manifest = _manifest(experiment)
    assessment = assess_batch4_manifest(manifest)

    assert assessment["ready"] is False
    assert blocker in assessment["blockers"]
    report = write_batch4_report(tmp_path, manifest, assessment)

    assert report["status"] == "SEALED_FAILED"
    assert report["qwen"]["calls"] == 0
    assert all(arm["scored_cases"] == 0 for arm in report["arms"].values())
    assert (tmp_path / "SEALED_FAILED").exists()
    assert (tmp_path / "waiting_qwen.json").exists() is (experiment == "E3")
