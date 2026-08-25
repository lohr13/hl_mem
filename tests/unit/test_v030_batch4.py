from __future__ import annotations

import importlib

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


@pytest.mark.parametrize(
    ("experiment", "expected"),
    [
        ("E2", {"invalid_pair_shape_cases", "unapproved_gold_cases", "invalid_clone_recall_metrics"}),
        ("E3", {"invalid_existing_extraction_set", "unapproved_gold_cases"}),
        ("E4", {"invalid_production_query_log", "unapproved_gold_cases"}),
    ],
)
def test_batch4_preflight_requires_explicit_gold_and_provenance(experiment: str, expected: set[str]) -> None:
    manifest = _manifest(experiment)
    manifest["source_audit"] = {"production_examples": 1, "clone_recall_metrics_sha256": "not-a-sha"}
    manifest["cases"][0]["input"] = {
        "text": "real grounded example",
        "query": "real entity query",
        "claims": [{"subject_canonical_entity_id": "agent:pony"}],
    }
    manifest["cases"][0]["gold"] = {"gold_status": "missing_or_unapproved"}
    manifest["cases"][0]["blind_judgment"] = {"decision": "equivalent"}

    assessment = assess_batch4_manifest(manifest)

    assert expected <= set(assessment["blockers"])


def test_e2_null_coordinate_values_are_never_eligible() -> None:
    manifest = _manifest("E2")
    manifest["cases"][0]["input"]["claims"] = [
        {
            "subject_canonical_entity_id": "agent:pony",
            "canonical_slot": None,
            "canonical_attribute": None,
            "assertion_kind": None,
            "entity_proof_id": None,
        }
        for _ in range(2)
    ]

    assessment = assess_batch4_manifest(manifest)

    assert assessment["counts"]["eligible_pairs"] == 0
    assert "missing_entity_proof_cases" in assessment["blockers"]


def test_v2_derivers_replace_placeholders_with_scored_contracts() -> None:
    module = importlib.import_module("hl_mem.evaluation.v030_batch4_v2_manifest")
    e2_case = _manifest("E2")["cases"][0]
    for claim in e2_case["input"]["claims"]:
        claim.update({"subject_entity_id": "same", "predicate": "fact", "value": "publish release", "qualifiers": {}})
    e2 = module.derive_e2_case(e2_case, {"judge_confidence": 0.99})
    e3_case = _manifest("E3")["cases"][0]
    e3_case["category"] = "correction"
    e3 = module.derive_e3_case(e3_case)
    e4 = module.derive_e4_case("unique_alias", 0, "project:hl_mem", ["claim-1"])

    assert e2["input"]["coordinate_proof"]["kind"] in {"typed_alias", "legacy_same_subject"}
    assert e2["input"]["claims"][0]["qualifiers"]["action_family"] == "publish"
    assert "????" not in e3["input"]["text"]
    assert e4["gold"]["entity_ids"] == ["project:hl_mem"]
    assert e4["gold"]["relevant_claim_ids"] == ["claim-1"]


def test_v2_e2_hard_validator_overrides_semantic_votes() -> None:
    replay = importlib.import_module("hl_mem.evaluation.v030_batch4_v2_replay")
    case = {
        "case_id": "unsafe",
        "input": {"historical_decision": "equivalent", "hard_validator": {"safe": False}},
    }

    frozen = replay.freeze_e2_gold(case, {"decision": "equivalent", "confidence": 1.0})

    assert frozen["gold"]["decision"] == "distinct"
    assert frozen["gold"]["gold_status"] == "blind_frozen"


def test_v2_e4_behavior_pass_cannot_override_synthetic_evidence_cap() -> None:
    replay = importlib.import_module("hl_mem.evaluation.v030_batch4_v2_replay")
    manifest = {
        "source_audit": {"synthetic_query_ratio": 1.0},
        "preregistration": {"synthetic_ratio_max": 0.5},
    }

    gate = replay.e4_release_gate(manifest, behavior_passed=True)

    assert gate == {"passed": False, "failure": "synthetic_ratio_over_preregistered_cap"}


def test_v2_volcano_pair_adapter_fills_current_required_columns() -> None:
    clone = importlib.import_module("hl_mem.evaluation.v030_e2_clone_replay")
    source = {"left_claim_id": "left", "right_claim_id": "right", "reviewed_at": "2026-08-25T00:00:00Z"}

    values = clone.prepare_export_values(
        "dedup_pairs", source, {"left_claim_id", "right_claim_id", "pair_key", "namespace_key", "created_at"}
    )

    assert values["pair_key"]
    assert values["namespace_key"] == "default"
    assert values["created_at"] == source["reviewed_at"]
