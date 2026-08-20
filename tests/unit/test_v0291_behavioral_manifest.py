from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from evaluation.v0291_behavioral.manifest import (
    expand_behavioral_samples,
    load_behavioral_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests/fixtures/v0291_freshness_behavioral.json"


def test_behavioral_manifest_expands_to_the_frozen_manual_gold_suite() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)

    assert manifest["eval_manifest_version"] == "v0291-behavioral-eval-v1"
    assert manifest["model_snapshot"] == "qwen3.7-plus-2026-05-26"
    assert len(samples) == 80
    assert Counter(sample["cohort"] for sample in samples) == {
        "incident": 20,
        "stale_positive": 20,
        "stable_negative": 20,
        "correction_backed": 10,
        "boundary": 10,
    }
    assert len({sample["opaque_sample_id"] for sample in samples}) == 80
    assert all(re.fullmatch(r"[0-9a-f]{32}", sample["opaque_sample_id"]) for sample in samples)
    assert all(sample["gold_source"] == "manual-review-2026-08-20" for sample in samples)
    assert all(sample["scenario_family_id"] for sample in samples)
    assert all(sample["user_prompt"] for sample in samples)
    assert all(sample["current_truth"] for sample in samples)
    assert all(sample["stale_or_stable_reference"] for sample in samples)
    assert all(isinstance(sample["allowed_verification_actions"], list) for sample in samples)
    assert all(isinstance(sample["harmful_or_write_actions"], list) for sample in samples)


def test_behavioral_manifest_contains_real_incident_tools_and_diverse_stale_domains() -> None:
    samples = expand_behavioral_samples(load_behavioral_manifest(MANIFEST_PATH))
    incidents = [sample for sample in samples if sample["cohort"] == "incident"]
    stale = [sample for sample in samples if sample["cohort"] == "stale_positive"]

    assert len({sample["user_prompt"] for sample in incidents}) == 20
    assert all("PyPI" in sample["current_truth"] for sample in incidents)
    assert all("inspect_python_install" in sample["allowed_verification_actions"] for sample in incidents)
    assert all(
        sample["deterministic_tool_results"]["inspect_python_install"]["result"]["install_source"] == "pypi"
        for sample in incidents
    )
    assert {sample["scenario_domain"] for sample in stale} == {
        "auth",
        "backup",
        "cli",
        "compatibility",
        "deployment",
        "migration",
        "path",
        "port",
        "release_sop",
        "worker",
    }


def test_stable_suite_spans_one_to_three_years_without_answer_leakage() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    stable = [sample for sample in samples if sample["cohort"] == "stable_negative"]
    rendering_now = datetime.fromisoformat(manifest["rendering_now"])
    ages = [(rendering_now - datetime.fromisoformat(sample["reference"]["recorded_from"])).days for sample in stable]

    assert min(ages) >= 365
    assert max(ages) >= 1095
    assert sum(sample["intent"] == "procedure" for sample in stable) >= 10
    assert {sample["stable_kind"] for sample in stable} == {
        "architecture",
        "constraint",
        "identity",
        "preference",
    }

    model_visible = "\n".join(
        f'{sample["user_prompt"]}\n{sample["reference"]["text"]}' for sample in samples
    ).casefold()
    for leaked_phrase in (
        "must verify",
        "remains authoritative",
        "correction-backed",
        "explicit replacement",
        "stale_positive",
        "stable_negative",
    ):
        assert leaked_phrase not in model_visible


def test_correction_age_only_and_boundary_gap_contracts_are_explicit() -> None:
    manifest = load_behavioral_manifest(MANIFEST_PATH)
    samples = expand_behavioral_samples(manifest)
    corrections = [sample for sample in samples if sample["cohort"] == "correction_backed"]
    boundaries = [sample for sample in samples if sample["cohort"] == "boundary"]

    assert all(sample["reference"]["source_kind"] == "correction_event" for sample in corrections)
    assert "verified_at" not in json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    assert {sample["boundary_kind"] for sample in boundaries} == {
        "active_recall",
        "bad_time",
        "fresh_age",
        "future_time",
        "historical_as_of",
        "missing_recorded_from",
        "missing_slot",
        "missing_tags",
        "packing_boundary",
        "unknown_assertion",
    }
    assert all(sample["expected_applicability"] == "boundary" for sample in boundaries)
