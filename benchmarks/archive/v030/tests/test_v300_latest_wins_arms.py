from __future__ import annotations

import importlib
import importlib.util
from typing import Any

MODULE = "benchmarks.archive.v030.tools.v0300_latest_wins_arms"


def _runner() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "arms runner module is missing"
    return importlib.import_module(MODULE)


def _corpus() -> dict[str, Any]:
    coordinate = {
        "namespace": "default",
        "canonical_subject": "project:hl_mem",
        "canonical_slot": "config.version",
        "coordinate_qualifiers": {},
    }
    return {
        "bundle_id": "fixture-001",
        "profile": "validation_a",
        "scenario": "newer_rollback",
        "subtype": "rollback",
        "source_kind": "real_deidentified",
        "existing_claim": {
            "claim_id": "old",
            "coordinate": coordinate,
            "value": "0.31.1",
            "assertion_kind": "observation",
            "event_time": "2026-08-26T01:00:00Z",
            "source_authority": 3,
            "source_id": "event-old",
        },
        "incoming_claim": {
            "claim_id": "new",
            "coordinate": coordinate,
            "value": "0.30.0",
            "assertion_kind": "observation",
            "event_time": "2026-08-26T02:00:00Z",
            "source_authority": 3,
            "source_id": "event-new",
        },
        "currentness_proof": {
            "schema_version": "status_report_v1",
            "producer_contract": "hl_mem.report-version-v1",
            "package": "hl_mem",
            "runtime_version": "0.30.0",
            "namespace": "default",
            "subject_proof": {"canonical_entity_id": "project:hl_mem", "alias_version": 1},
            "observed_at": "2026-08-26T02:00:00Z",
        },
        "chain_state": {"current_tip_count": 1, "old_tip_status": "active", "acyclic": True},
    }


def test_arms_emit_hermes_fields_and_keep_a_off_while_b_resolves() -> None:
    runner = _runner()
    corpus = [_corpus()]
    gold = {"fixture-001": {"expected_temporal_relation": "supersedes_existing"}}

    arm_a = runner.run_arm(corpus, gold, "A")
    arm_b = runner.run_arm(corpus, gold, "B")

    assert arm_a[0]["actual_relation"] == "compatible"
    assert arm_a[0]["reason"] == "mode_off"
    assert {
        key: arm_b[0][key]
        for key in (
            "bundle_id",
            "expected_temporal_relation",
            "actual_relation",
            "rule_id",
            "reason",
        )
    } == {
        "bundle_id": "fixture-001",
        "expected_temporal_relation": "supersedes_existing",
        "actual_relation": "supersedes_existing",
        "rule_id": "state-latest-wins-v1:supersedes_existing",
        "reason": "event_time_direction",
    }


def test_gate_summary_uses_literal_numerators_and_denominators() -> None:
    runner = _runner()
    gold = {"fixture-001": {"expected_temporal_relation": "supersedes_existing"}}
    records = runner.run_arm([_corpus()], gold, "B")

    summary = runner.summarize(records)

    assert summary["automatic_edge_precision"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert summary["eligible_recall"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert summary["counterexample_false_supersede"] == {"numerator": 0, "denominator": 0}
    assert summary["real_cohort_stale_reduction"] == {"numerator": 1, "denominator": 1, "value": 1.0}


def test_three_replays_are_compared_before_any_arm_is_written() -> None:
    runner = _runner()
    gold = {"fixture-001": {"expected_temporal_relation": "supersedes_existing"}}

    records, digests = runner.replay_arm([_corpus()], gold, "B")

    assert len(records) == 1
    assert len(digests) == 3
    assert len(set(digests)) == 1
