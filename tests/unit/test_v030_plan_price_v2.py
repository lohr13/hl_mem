from __future__ import annotations

from hl_mem.domain.instruments import InstrumentReference
from hl_mem.evaluation.v030_plan_price_corpus import (
    assess_e5_v2_manifest,
    assess_e6_v2_manifest,
    derive_e5_case,
    derive_e6_pair,
)
from hl_mem.evaluation.v030_plan_price_manifest import synthetic_e6_cases


def _reference(entity_id: str = "instrument:01", ticker: str = "T01") -> InstrumentReference:
    return InstrumentReference(entity_id, f"US:NASDAQ:{ticker}", ((f"NASDAQ:{ticker}", 1),))


def test_e5_derivation_uses_action_and_instrument_parsers() -> None:
    case = {
        "case_id": "e5:complete:001",
        "category": "complete",
        "source": "synthetic_contract",
        "input": {
            "plan": {"target": "instrument:01", "quantity": "101", "unit": "?", "account": None},
            "result": {"target": "instrument:01", "quantity": "101", "unit": "?", "account": None},
        },
        "gold": {"decision": "complete"},
    }

    derived = derive_e5_case(case, [_reference()])

    plan = derived["input"]["plan"]
    result = derived["input"]["result"]
    assert plan["action_family"] == result["action_family"] == "open"
    assert plan["assertion_phase"] == "plan"
    assert result["assertion_phase"] == "execution"
    assert plan["direction"] == result["direction"] == "long"
    assert plan["quantity_mode"] == result["quantity_mode"] == "exact"
    assert plan["quantity"] == result["quantity"] == "101"
    assert plan["unit"] == result["unit"] == "unit"
    assert plan["canonical_target_entity_id"] == result["canonical_target_entity_id"] == "instrument:01"
    assert derived["derivation"]["action_parser"] == "project_action_qualifiers"
    assert derived["derivation"]["target_parser"] == "resolve_instrument_target"


def test_e5_v2_preflight_allows_at_most_ten_percent_unresolved() -> None:
    cases = []
    categories = [
        *("complete" for _ in range(35)),
        *("cancel" for _ in range(25)),
        *("replace" for _ in range(25)),
        *("partial" for _ in range(30)),
        *("ambiguous_negative" for _ in range(25)),
    ]
    for index, category in enumerate(categories):
        coordinate = {
            "action_family": "open",
            "assertion_phase": "plan",
            "canonical_target_entity_id": "instrument:test",
            "direction": "long",
            "quantity": "100",
            "quantity_mode": "exact",
            "unit": "unit",
        }
        result = {**coordinate, "assertion_phase": "execution"}
        cases.append(
            {
                "case_id": f"case:{index:03d}",
                "category": category,
                "source": "synthetic_contract_v2",
                "input": {"plan": coordinate, "result": result},
                "gold": {"decision": "ambiguous" if category == "ambiguous_negative" else category},
            }
        )
    manifest = {
        "experiment": "E5",
        "schema_version": "v030-corpus-1",
        "source_snapshots": [{"source_id": "fixture", "sha256": "a" * 64, "reconstructable": True}],
        "cases": cases,
        "preregistration": {"max_coordinate_missing_rate": 0.10},
    }

    assessment = assess_e5_v2_manifest(manifest)

    assert assessment["ready"] is True
    assert assessment["counts"]["coordinate_missing_rate"] == 0.0


def test_e6_derivation_builds_two_complete_price_coordinates() -> None:
    left = {
        "id": "left",
        "subject_entity_id": "NASDAQ:T01",
        "value": "NASDAQ:T01 close price USD 10.00 on 2026-08-18",
        "valid_from": "2026-08-18T15:00:00+00:00",
    }
    right = {
        "id": "right",
        "subject_entity_id": "NASDAQ:T01",
        "value": "NASDAQ:T01 close price USD 11.00 on 2026-08-19",
        "valid_from": "2026-08-19T15:00:00+00:00",
    }

    case = derive_e6_pair(
        "pair:1",
        left,
        right,
        [_reference()],
        source="volcano_conflict",
        label_provenance={"kind": "conflict_case", "decision": "keep_right", "id": "conflict:1"},
    )

    assert case["input"]["left"]["canonical_target_entity_id"] == "instrument:01"
    assert case["input"]["right"]["canonical_target_entity_id"] == "instrument:01"
    assert case["input"]["left"]["price_axis"] == case["input"]["right"]["price_axis"] == "close"
    assert case["input"]["left"]["snapshot_date"] == "2026-08-18"
    assert case["input"]["right"]["snapshot_date"] == "2026-08-19"
    assert case["gold"]["decision"] == "snapshot_advance"
    assert case["gold"]["label_provenance"]["id"] == "conflict:1"


def test_e6_v2_preflight_rejects_more_than_ten_percent_missing_targets() -> None:
    pair = {
        "price_axis": "close",
        "canonical_target_entity_id": "instrument:test",
        "snapshot_date": "2026-08-18",
    }
    cases = [
        {
            "case_id": f"e6:{index:03d}",
            "category": "price",
            "source": "synthetic_contract_v2",
            "instrument_id": f"instrument:{index % 52:02d}",
            "input": {"left": dict(pair), "right": dict(pair)},
            "gold": {"decision": "uncertain"},
        }
        for index in range(120)
    ]
    for case in cases[:13]:
        case["input"]["left"]["canonical_target_entity_id"] = None
    manifest = {
        "experiment": "E6",
        "schema_version": "v030-corpus-1",
        "source_snapshots": [{"source_id": "fixture", "sha256": "a" * 64, "reconstructable": True}],
        "cases": cases,
        "preregistration": {"max_target_missing_rate": 0.10},
    }

    assessment = assess_e6_v2_manifest(manifest)

    assert assessment["ready"] is False
    assert assessment["counts"]["target_missing_cases"] == 13
    assert assessment["blockers"] == ["target_missing_rate_exceeded"]


def test_e6_synthetic_fill_preserves_52_instruments_and_ten_percent_missing() -> None:
    references = [
        InstrumentReference(
            f"instrument:synthetic:{index:02d}",
            f"US:NASDAQ:X{index:02d}",
            ((f"NASDAQ:X{index:02d}", 1),),
        )
        for index in range(52)
    ]

    cases = synthetic_e6_cases(references, count=120, unresolved_count=12)

    assert len(cases) == 120
    assert len({case["instrument_id"] for case in cases}) == 52
    assert (
        sum(
            not case["input"]["left"]["canonical_target_entity_id"]
            or not case["input"]["right"]["canonical_target_entity_id"]
            for case in cases
        )
        == 12
    )
    assert all(case["gold"]["decision"] == "uncertain" for case in cases[:12])
