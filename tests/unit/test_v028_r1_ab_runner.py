from __future__ import annotations

import importlib
import json

import pytest

from hl_mem.storage.database import Database

runner = importlib.import_module("evaluation.tools.run_v028_relation_semantics_ab")


def _metrics(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "claim_yield_per_event": 2.0,
        "nonrelation_claim_yield_per_event": 2.0,
        "canonical_slot_mismatch_rate": 0.0,
        "entity_coverage_at_5": 0.35,
        "proposal_visible_edge_coverage": 0.50,
        "legacy_anchor_coverage": {"C0": 0.929, "C4": 0.929},
        "forbidden_violations": 0,
        "modality_violations": 0,
        "provenance_violations": 0,
        "leakage_violations": 0,
        "proposal_schema_failure_rate": 0.0,
        "retry_rate": 0.0,
        "exact_rao_rate": 0.0,
        "packet_rao_completeness": 0.0,
        "source_bounded_acceptance_rate": 0.0,
        "accepted_source_boundary_precision": 1.0,
        "source_semantics_without_edge_rate": 0.0,
        "expansion_eligible_edge_coverage": 0.50,
        "expansion_eligible_edge_cases": 5,
    }
    value.update(overrides)
    return value


def _diagnostics(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "cache_hashes_identical": True,
        "claim_identity_identical": True,
        "c0_packet_claim_preservation": True,
        "baseline_claim_displacements": 0,
        "ordinary_anchor_correct_to_missing": 0,
        "packet_smoke_passed": True,
        "input_token_ratio": 1.20,
        "output_token_ratio": 1.30,
        "logical_calls": 340,
    }
    value.update(overrides)
    return value


def test_paired_task_order_is_deterministic_and_interleaves_arms() -> None:
    sources = {"t1": ["a", "b"], "t2": ["c"]}

    first = runner.paired_task_order(sources, preregistration_id="frozen")
    second = runner.paired_task_order(sources, preregistration_id="frozen")

    assert first == second
    assert {(task["trajectory_id"], task["source_claim_id"], task["arm"]) for task in first} == {
        ("t1", "a", "R0"),
        ("t1", "a", "R1"),
        ("t1", "b", "R0"),
        ("t1", "b", "R1"),
        ("t2", "c", "R0"),
        ("t2", "c", "R1"),
    }
    assert [task["arm"] for task in first] not in (["R0", "R0", "R0", "R1", "R1", "R1"],)


def test_round_two_freezes_the_current_reproducible_source_count_without_padding() -> None:
    assert runner.EXPECTED_SOURCE_COUNT == 168
    assert runner.EXPECTED_CALLS == 336


def test_pilot_gate_allows_formal_run_only_after_three_calls_persist_an_annotation() -> None:
    runner.assert_pilot_gate(
        "frozen-prereg",
        {
            "preregistration_sha256": "frozen-prereg",
            "attempted": 3,
            "accepted": 1,
            "persisted": 1,
            "calls": [{"status": "accepted"}, {"status": "not_provided"}, {"status": "missing_component"}],
        },
    )


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "preregistration_sha256": "other-prereg",
            "attempted": 3,
            "accepted": 1,
            "persisted": 1,
            "calls": [{}, {}, {}],
        },
        {
            "preregistration_sha256": "frozen-prereg",
            "attempted": 2,
            "accepted": 1,
            "persisted": 1,
            "calls": [{}, {}],
        },
        {
            "preregistration_sha256": "frozen-prereg",
            "attempted": 3,
            "accepted": 0,
            "persisted": 0,
            "calls": [{}, {}, {}],
        },
    ],
)
def test_pilot_gate_rejects_drift_incomplete_calls_or_zero_acceptance(artifact) -> None:
    with pytest.raises(RuntimeError, match="pilot"):
        runner.assert_pilot_gate("frozen-prereg", artifact)


def test_scrub_relation_discovery_effects_restores_only_cases_created_by_proposals(tmp_path) -> None:
    database = Database(tmp_path / "scrub.db")
    connection = database.open()
    try:
        for claim_id, status in (
            ("source", "disputed"),
            ("target", "disputed"),
            ("left", "disputed"),
            ("right", "disputed"),
        ):
            connection.execute(
                "INSERT INTO claims(id,namespace_key,subject_entity_id,predicate,value_json,status,confidence,recorded_from) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (claim_id, "default", claim_id, "fact", json.dumps(claim_id), status, 1.0, "2026-01-01Z"),
            )
        connection.execute(
            "INSERT INTO memory_relations(id,from_id,to_id,relation,confidence,created_at,valid_from) "
            "VALUES ('edge','source','target','supports',0.9,'2026-02-01Z','2026-02-01Z')"
        )
        connection.execute(
            "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,confidence,created_at) "
            "VALUES ('created-case','created-pair','source','target','manual_required','contradicts','new',0.9,'2026-02-01Z')"
        )
        connection.execute(
            "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,confidence,created_at) "
            "VALUES ('preexisting-case','old-pair','left','right','manual_required','contradicts','old',0.9,'2026-01-01Z')"
        )
        connection.execute(
            "INSERT INTO relation_proposals(id,run_id,source_claim_id,target_claim_id,relation,confidence,rationale,"
            "supporting_claim_ids_json,model,mode,status,decision_reason,relation_id,conflict_case_id,created_at,decided_at) "
            "VALUES ('proposal','run','source','target','contradicts',0.9,'new','[]','fake','auto',"
            "'conflict_created','contradiction_threshold','edge','created-case','2026-02-01Z','2026-02-01Z')"
        )
        connection.commit()

        counts = runner.scrub_relation_discovery_effects(connection)

        assert counts == {"relations": 1, "proposals": 1, "created_conflicts": 1, "reactivated_claims": 2}
        assert connection.execute("SELECT count(*) FROM memory_relations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM relation_proposals").fetchone()[0] == 0
        assert connection.execute("SELECT id FROM conflict_cases").fetchone()[0] == "preexisting-case"
        statuses = {row["id"]: row["status"] for row in connection.execute("SELECT id,status FROM claims")}
        assert statuses == {"source": "active", "target": "active", "left": "disputed", "right": "disputed"}
    finally:
        database.close()


def test_three_layer_gates_pass_only_with_base_preservation_and_c4_readiness() -> None:
    r0 = _metrics()
    r1 = _metrics(
        source_bounded_acceptance_rate=0.85,
        exact_rao_rate=0.30,
        packet_rao_completeness=0.25,
        source_semantics_without_edge_rate=0.20,
        expansion_eligible_edge_coverage=0.85,
        expansion_eligible_edge_cases=7,
    )

    gates = runner.evaluate_three_layer_gates(r0, r1, _diagnostics())

    assert gates["passed"] is True
    assert gates["sealed_v3_eligible"] is True
    assert all(layer["passed"] for layer in gates["layers"])


def test_three_layer_gates_separate_semantics_from_sealed_eligibility() -> None:
    r0 = _metrics()
    r1 = _metrics(
        source_bounded_acceptance_rate=0.85,
        exact_rao_rate=0.30,
        packet_rao_completeness=0.25,
        source_semantics_without_edge_rate=0.20,
        expansion_eligible_edge_coverage=0.60,
        expansion_eligible_edge_cases=6,
    )

    gates = runner.evaluate_three_layer_gates(r0, r1, _diagnostics(packet_smoke_passed=False))

    assert gates["passed"] is False
    assert gates["semantics_gate_passed"] is True
    assert gates["sealed_v3_eligible"] is False
    relation_layer = next(layer for layer in gates["layers"] if layer["id"] == "relation_effectiveness")
    failed = {check["id"] for check in relation_layer["checks"] if not check["passed"]}
    assert {"expansion_edge_coverage", "c0_c4_packet_smoke"}.issubset(failed)


def test_three_layer_gates_reject_any_anchor_or_safety_regression() -> None:
    r0 = _metrics()
    r1 = _metrics(
        source_bounded_acceptance_rate=0.85,
        exact_rao_rate=0.30,
        packet_rao_completeness=0.25,
        source_semantics_without_edge_rate=0.20,
        expansion_eligible_edge_coverage=0.85,
        expansion_eligible_edge_cases=7,
        legacy_anchor_coverage={"C0": 0.929, "C4": 0.90},
        forbidden_violations=1,
    )

    gates = runner.evaluate_three_layer_gates(r0, r1, _diagnostics(ordinary_anchor_correct_to_missing=1))

    assert gates["passed"] is False
    failed = {check["id"] for layer in gates["layers"] for check in layer["checks"] if not check["passed"]}
    assert {"safety_zero", "legacy_anchor_floor", "ordinary_anchor_zero_regression"}.issubset(failed)
