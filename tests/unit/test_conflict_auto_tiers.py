from __future__ import annotations

import pytest

from hl_mem.domain.governance import L1Policy, decide_l1
from hl_mem.workers.auto_resolve_conflicts import decide_l0


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
            "entity_type_mismatch": False,
            "coordinates_complete": True,
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
