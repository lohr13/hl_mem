from __future__ import annotations

from typing import Any

import pytest

from hl_mem.evaluation.state_experiment_scoring import score_protocol
from hl_mem.evaluation.state_product_adapter import bind_product_evidence

COORDINATE = {
    "namespace": "default",
    "canonical_subject": "gateway",
    "canonical_slot": "config.version",
    "coordinate_qualifiers": {},
}


def _raw(value: str, evidence: str, indices: list[int] | None = None) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "subject": "gateway",
        "predicate": "version",
        "value": value,
        "evidence_quote": evidence,
    }
    if indices is not None:
        claim["source_event_indices"] = indices
    return claim


def _product(value: str, indices: list[int]) -> dict[str, Any]:
    return {"value": value, "source_event_indices": indices}


def test_missing_raw_source_indices_uses_single_event_production_default() -> None:
    raw = _raw("v1.0", "gateway version v1.0")

    bindings = bind_product_evidence([raw], [_product("v1.0", [0])], event_count=1)

    assert len(bindings) == 1
    assert bindings[0].raw_claim_index == 0
    assert bindings[0].raw_claim is raw


def test_explicit_source_indices_are_matched_without_defaulting() -> None:
    raw = _raw("v1.1", "gateway version v1.1", [1])

    binding = bind_product_evidence([raw], [_product("v1.1", [1])], event_count=2)[0]

    assert binding.raw_claim is raw


def test_missing_source_indices_in_multi_event_response_fails_closed() -> None:
    with pytest.raises(ValueError, match="omits source_event_indices for a multi-event bundle"):
        bind_product_evidence(
            [_raw("v1.0", "gateway version v1.0")],
            [_product("v1.0", [0])],
            event_count=2,
        )


def test_same_source_claims_bind_by_exact_value_not_position() -> None:
    raw_v1 = _raw("v1.0", "gateway version v1.0", [0])
    raw_v2 = _raw("v2.0", "gateway version v2.0", [0])

    bindings = bind_product_evidence(
        [raw_v1, raw_v2],
        [_product("v2.0", [0]), _product("v1.0", [0])],
        event_count=1,
    )

    assert [binding.raw_claim for binding in bindings] == [raw_v2, raw_v1]


def test_rejected_raw_claim_does_not_shift_product_evidence() -> None:
    rejected = _raw("planned-v2", "we plan gateway v2", [0])
    accepted = _raw("v1.0", "gateway version v1.0", [0])

    binding = bind_product_evidence(
        [rejected, accepted],
        [_product("v1.0", [0])],
        event_count=1,
    )[0]

    assert binding.raw_claim_index == 1
    assert binding.raw_claim is accepted


@pytest.mark.parametrize(
    ("raw_claims", "message"),
    [
        ([_raw("v9.9", "gateway version v9.9", [0])], "cannot be matched"),
        (
            [
                _raw("v1.0", "gateway version v1.0", [0]),
                _raw("v1.0", "a second gateway version v1.0", [0]),
            ],
            "ambiguous raw evidence",
        ),
    ],
    ids=("value-mismatch", "duplicate-ambiguity"),
)
def test_value_mismatch_and_duplicate_raw_evidence_fail_closed(raw_claims: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        bind_product_evidence(raw_claims, [_product("v1.0", [0])], event_count=1)


def test_bound_tampered_evidence_still_loses_semantic_credit() -> None:
    raw = _raw("v1.0", "invented evidence v1.0")
    binding = bind_product_evidence([raw], [_product("v1.0", [0])], event_count=1)[0]
    assertion_id = "adapter-fixture:c0:a0"
    candidate = [
        {
            "sample_id": "adapter-fixture",
            "claims": [
                {
                    "assertion_id": assertion_id,
                    "claim": {
                        "source_event_indices": [0],
                        "value": binding.raw_claim["value"],
                        "evidence_quote": binding.raw_claim["evidence_quote"],
                    },
                    "projection": {"coordinate": COORDINATE},
                }
            ],
        }
    ]
    gold = [
        {
            "bundle_id": "adapter-fixture",
            "category": "software_version",
            "atomic_claims": [
                {
                    "assertion_id": assertion_id,
                    "source_event_indices": [0],
                    "state_value": "v1.0",
                    "coordinate": COORDINATE,
                }
            ],
        }
    ]

    report = score_protocol(
        gold,
        baseline_predictions={"claim_count": 1},
        candidate_runs=[candidate, candidate, candidate],
        persisted_edges=set(),
        baseline_observations={"current_injected_assertion_ids": []},
        candidate_observations={"current_injected_assertion_ids": []},
        corpus_records=[
            {
                "bundle_id": "adapter-fixture",
                "category": "software_version",
                "subtype": "upgrade",
                "events": [{"event_index": 0, "content": {"text": "gateway version v1.0"}}],
            }
        ],
    )

    assert report["metrics"]["atomic_claim"]["true_positive"] == 0
    assert report["metrics"]["atomic_claim"]["false_positive"] == 1
    assert report["metrics"]["atomic_claim"]["false_negative"] == 1
    assert report["mapping_diagnostics"]["semantic_rejections"] == {"evidence_ungrounded": 1}
