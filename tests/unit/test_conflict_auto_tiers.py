from __future__ import annotations

import pytest

from hl_mem.workers.auto_resolve_conflicts import (
    L1Policy,
    assess_l2_admission,
    decide_l0,
    decide_l1,
    validate_l2_result,
)

NOW = "2026-08-25T08:00:00+00:00"


def _docket() -> dict[str, object]:
    return {
        "case": {
            "id": "case-1",
            "namespace_key": "default",
            "group_key": None,
            "revision": 3,
            "overflow": 0,
        },
        "claims": [
            {
                "id": "left",
                "status": "disputed",
                "namespace_key": "default",
                "subject_entity_id": "gateway",
                "canonical_slot": "config.port",
                "qualifiers": {"service": "gateway"},
                "value": "8080",
                "source_authority": "medium",
                "confidence": 0.9,
                "valid_from": "2026-08-25T06:00:00+00:00",
                "valid_to": None,
                "recorded_from": "2026-08-25T06:01:00+00:00",
                "assertion_kind": "observation",
            },
            {
                "id": "right",
                "status": "disputed",
                "namespace_key": "default",
                "subject_entity_id": "gateway",
                "canonical_slot": "config.port",
                "qualifiers": {"service": "gateway"},
                "value": "8081",
                "source_authority": "medium",
                "confidence": 0.9,
                "valid_from": "2026-08-25T06:00:00+00:00",
                "valid_to": None,
                "recorded_from": "2026-08-25T06:01:00+00:00",
                "assertion_kind": "observation",
            },
        ],
        "candidates": [
            {"candidate_key": "left", "representative_claim_id": "left", "support_count": 1, "evidence_count": 1},
            {
                "candidate_key": "right",
                "representative_claim_id": "right",
                "support_count": 1,
                "evidence_count": 1,
            },
        ],
        "evidence": [{"id": "evidence-left", "claim_id": "left"}],
        "context": {
            "left_tip_id": "left",
            "right_tip_id": "right",
            "survivor_contested": False,
            "schema_valid": True,
            "evidence_readable": True,
            "entity_type_mismatch": False,
            "coordinates_complete": True,
            "equal_authority_first_hand_conflict": False,
            "previous_reason": "l0_l1_insufficient",
            "last_l2_policy_version": None,
            "not_before": None,
            "docket_oversized": False,
        },
    }


@pytest.mark.parametrize(
    ("mutate", "decision", "rule"),
    [
        (lambda d: d["context"].update(left_tip_id="tip", right_tip_id="tip"), "obsolete", "chain_endpoint_converged"),
        (lambda d: d["claims"][0].update(status="superseded"), "keep_right", "lifecycle_single_survivor"),
        (lambda d: d["claims"][1].update(value="8080"), "keep_left", "exact_candidate"),
        (
            lambda d: (
                d["claims"][0].update(valid_from="2026-08-25T05:00:00+00:00", valid_to="2026-08-25T05:30:00+00:00"),
                d["claims"][1].update(valid_from="2026-08-25T06:00:00+00:00"),
            ),
            "keep_right",
            "strict_temporal_state_change",
        ),
        (lambda d: d["claims"][0].update(source_authority="high"), "keep_left", "strict_authority"),
        (
            lambda d: (
                [claim.update(canonical_slot=None, qualifiers={}) for claim in d["claims"]],
                d["context"].update(nonexclusive_false_positive=True),
            ),
            "coexist",
            "nonexclusive_false_positive",
        ),
    ],
)
def test_l0_rules_run_in_fixed_order(mutate, decision: str, rule: str) -> None:
    docket = _docket()
    mutate(docket)

    result = decide_l0(docket)

    assert (result.decision, result.rule, result.tier) == (decision, rule, "L0")


@pytest.mark.parametrize("status", ["superseded", "expired", "rejected", "rolled_back"])
def test_l0_lifecycle_uses_only_the_four_registered_terminal_statuses(status: str) -> None:
    docket = _docket()
    docket["claims"][0]["status"] = status

    result = decide_l0(docket)

    assert result is not None
    assert (result.decision, result.rule) == ("keep_right", "lifecycle_single_survivor")


@pytest.mark.parametrize("status", ["disputed", "retracted", "archived"])
def test_l0_does_not_treat_unregistered_or_disputed_status_as_terminal(status: str) -> None:
    docket = _docket()
    docket["claims"][0]["status"] = status

    assert decide_l0(docket) is None


@pytest.mark.parametrize(
    ("mutate", "rule", "winner"),
    [
        (
            lambda d: d["claims"][0].update(valid_from="2026-08-25T07:00:00+00:00", confidence=0.95),
            "temporal_confidence_dominance",
            "left",
        ),
        (
            lambda d: d["claims"][0].update(source_authority="high", confidence=1.0),
            "authority_evidence_dominance",
            "left",
        ),
        (
            lambda d: d["candidates"][0].update(support_count=3, evidence_count=3),
            "group_support_dominance",
            "left",
        ),
    ],
)
def test_l1_conjunctive_rules(mutate, rule: str, winner: str) -> None:
    docket = _docket()
    mutate(docket)

    result = decide_l1(docket, L1Policy(min_time_delta_seconds=300, min_confidence_delta=0.1))

    assert result is not None
    assert (result.rule, result.winner_candidate_key, result.tier) == (rule, winner, "L1")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["context"].update(entity_type_mismatch=True),
        lambda d: d["context"].update(coordinates_complete=False),
        lambda d: d["case"].update(overflow=1),
        lambda d: d["claims"][0].update(assertion_kind="plan"),
    ],
)
def test_l1_refuses_hard_safety_gaps(mutation) -> None:
    docket = _docket()
    docket["claims"][0].update(source_authority="high", confidence=1.0)
    mutation(docket)

    assert decide_l1(docket, L1Policy(0, 0.1)) is None


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda d: d["context"].update(previous_reason=None), "missing_prior_insufficiency"),
        (lambda d: d.update(candidates=d["candidates"][:1]), "fewer_than_two_living_candidates"),
        (lambda d: d["case"].update(overflow=1), "candidate_overflow"),
        (lambda d: d["context"].update(entity_type_mismatch=True), "entity_type_mismatch"),
        (lambda d: d["context"].update(evidence_readable=False), "evidence_damaged"),
        (lambda d: d["context"].update(last_l2_policy_version="conflict-auto-v1"), "already_judged_current_input"),
        (lambda d: d["context"].update(schema_valid=False), "schema_invalid"),
    ],
)
def test_l2_admission_is_fail_closed_for_each_condition(mutation, reason: str) -> None:
    docket = _docket()
    mutation(docket)

    result = assess_l2_admission(docket, NOW, max_candidates=8, policy_version="conflict-auto-v1")

    assert (result.admitted, result.reason) == (False, reason)


def test_l2_admits_a_slotless_pair_when_claims_and_evidence_are_complete() -> None:
    docket = _docket()
    docket["context"]["coordinates_complete"] = False
    docket["claims"][0]["canonical_slot"] = None
    docket["claims"][1]["canonical_slot"] = None

    result = assess_l2_admission(docket, NOW, max_candidates=8, policy_version="conflict-auto-v1")

    assert (result.admitted, result.reason) == (True, "admitted")


def test_l2_rejects_plan_without_calling_it_a_coordinate_failure() -> None:
    docket = _docket()
    docket["claims"][0]["assertion_kind"] = "plan"

    result = assess_l2_admission(docket, NOW, max_candidates=8, policy_version="conflict-auto-v1")

    assert (result.admitted, result.reason) == (False, "plan_not_allowed")


@pytest.mark.parametrize(
    ("result_update", "docket_update", "enabled", "rule"),
    [
        ({"consistent": False}, {}, True, "candidate_order_disagreement"),
        ({"confidence": 0.89}, {}, True, "low_confidence"),
        ({}, {"equal_authority_first_hand_conflict": True}, True, "equal_authority_counterevidence"),
        ({"winner_candidate_key": "missing"}, {}, True, "winner_membership_violation"),
        ({}, {"docket_oversized": True}, True, "oversized_docket"),
        ({"decision": "coexist"}, {}, True, "exclusive_group_violation"),
        ({}, {}, False, "rule_not_enabled"),
    ],
)
def test_l3_conditions_have_stable_rules(result_update, docket_update, enabled: bool, rule: str) -> None:
    docket = _docket()
    docket["context"].update(docket_update)
    result = {
        "consistent": True,
        "decision": "keep_left",
        "winner_candidate_key": "left",
        "confidence": 0.95,
        **result_update,
    }

    decision = validate_l2_result(docket, result, confidence_floor=0.9, rule_enabled=enabled)

    assert (decision.decision, decision.rule, decision.tier) == ("manual_required", rule, "L3")


def test_l2_valid_result_preserves_only_short_structured_fields() -> None:
    result = validate_l2_result(
        _docket(),
        {
            "consistent": True,
            "decision": "keep_left",
            "winner_candidate_key": "left",
            "confidence": 0.95,
            "decisions": [
                {"rationale_code": "newer_evidence", "decisive_evidence_ids": ["evidence-left"]},
                {"rationale_code": "newer_evidence", "decisive_evidence_ids": ["evidence-left"]},
            ],
        },
        confidence_floor=0.9,
        rule_enabled=True,
    )

    assert result.tier == "L2"
    assert result.evidence_ids == ("evidence-left",)
    assert not hasattr(result, "chain_of_thought")
