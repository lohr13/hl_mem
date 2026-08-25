from __future__ import annotations

from hl_mem.domain.instruments import InstrumentReference
from hl_mem.evaluation.local_qwen_runner import LocalQwenRunner
from hl_mem.evaluation.v030_plan_price_corpus import (
    assess_e5_v2_manifest,
    assess_e6_v2_manifest,
    derive_e5_case,
    derive_e6_pair,
)
from hl_mem.evaluation.v030_plan_price_manifest import synthetic_e6_cases
from hl_mem.evaluation.v030_plan_price_replay import (
    deterministic_e5_decision,
    qwen_e5_docket,
    request_hashes_for_docket,
    score_e5_predictions,
    score_e6_predictions,
)


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


def test_deterministic_e5_decision_enforces_protected_coordinates() -> None:
    plan = {
        "action_family": "open",
        "assertion_phase": "plan",
        "canonical_target_entity_id": "instrument:test",
        "direction": "long",
        "quantity_mode": "exact",
        "quantity": "100.0",
        "unit": "share",
        "account": "A",
    }
    result = {**plan, "assertion_phase": "execution", "quantity": "40"}

    assert deterministic_e5_decision({"input": {"plan": plan, "result": result}}) == "partial"
    assert deterministic_e5_decision({"input": {"plan": plan, "result": {**result, "quantity": "101"}}}) == "ambiguous"
    assert deterministic_e5_decision({"input": {"plan": plan, "result": {**result, "account": "B"}}}) == "ambiguous"
    assert deterministic_e5_decision({"input": {"plan": plan, "result": {**result, "negated": True}}}) == "ambiguous"


def test_qwen_e5_docket_is_gold_free_and_order_permutable() -> None:
    case = {
        "case_id": "e5:test",
        "input": {
            "plan": {"text": "plan to buy 100 units NASDAQ:T01", "quantity": "100"},
            "result": {"text": "executed buy 40 units NASDAQ:T01", "quantity": "40"},
        },
        "gold": {"decision": "partial"},
    }

    docket = qwen_e5_docket(case)

    assert "gold" not in str(docket).casefold()
    assert [item["candidate_key"] for item in docket["candidates"]] == ["plan", "result"]
    assert docket["case_id"] == "e5:test"


def test_qwen_v2_docket_completes_two_permuted_fake_calls() -> None:
    snapshots = []

    def transport(_url: str, payload: dict) -> dict:
        snapshots.append(payload)
        return {"decision": "partial", "confidence": 0.99}

    runner = LocalQwenRunner(token_counter=lambda text: len(text) // 4, transport=transport)
    docket = qwen_e5_docket(
        {
            "case_id": "e5:fake",
            "input": {
                "plan": {"text": "plan to buy 100 units NASDAQ:T01", "quantity": "100"},
                "result": {"text": "executed buy 40 units NASDAQ:T01", "quantity": "40"},
            },
            "gold": {"decision": "partial"},
        }
    )

    result = runner.run_case(docket)

    assert result["consistent"] is True
    assert result["call_count"] == 2
    assert len(snapshots) == 2
    assert all(snapshot["chat_template_kwargs"] == {"enable_thinking": False} for snapshot in snapshots)
    first = snapshots[0]["messages"][1]["content"]
    second = snapshots[1]["messages"][1]["content"]
    assert first.index('"candidate_key":"plan"') < first.index('"candidate_key":"result"')
    assert second.index('"candidate_key":"result"') < second.index('"candidate_key":"plan"')


def test_qwen_request_hashes_allow_only_exact_docket_reuse() -> None:
    runner = LocalQwenRunner(
        token_counter=lambda text: len(text.encode("utf-8")),
        transport=lambda _url, _payload: {"decision": "partial", "confidence": 0.99},
    )
    docket = {
        "case_id": "reuse",
        "instruction": "classify",
        "candidates": [{"candidate_key": "plan"}, {"candidate_key": "result"}],
        "evidence": [],
    }

    original = request_hashes_for_docket(docket, runner)
    changed = request_hashes_for_docket({**docket, "instruction": "changed"}, runner)

    assert len(original) == 2
    assert original != changed


def test_e5_scorer_enforces_all_preregistered_gates() -> None:
    outcomes = ["complete"] * 35 + ["cancel"] * 25 + ["replace"] * 25 + ["partial"] * 30 + ["ambiguous"] * 25
    cases = [
        {
            "case_id": f"e5:{index}",
            "input": {
                "plan": {"quantity_mode": "exact", "quantity": "100", "unit": "share"},
                "result": {
                    "quantity_mode": "exact",
                    "quantity": "50" if outcome == "partial" else "100",
                    "unit": "share",
                },
            },
            "gold": {"decision": outcome},
        }
        for index, outcome in enumerate(outcomes)
    ]
    predictions = [{"case_id": case["case_id"], "decision": case["gold"]["decision"]} for case in cases]

    score = score_e5_predictions(cases, predictions)

    assert score["gate"]["passed"] is True
    assert score["metrics"]["macro_f1"] == 1.0
    assert score["metrics"]["partial_quantity_conservation"] == 1.0
    assert score["metrics"]["error_closures"] == 0


def test_e6_scorer_counts_target_coverage_and_cross_target_supersede() -> None:
    cases = [
        {
            "case_id": "cross",
            "risk_tags": ["cross_target"],
            "input": {
                "left": {"canonical_target_entity_id": "instrument:A"},
                "right": {"canonical_target_entity_id": "instrument:B"},
            },
            "gold": {"decision": "distinct_series"},
        },
        {
            "case_id": "missing",
            "input": {
                "left": {"canonical_target_entity_id": None},
                "right": {"canonical_target_entity_id": None},
            },
            "gold": {"decision": "uncertain"},
        },
    ]
    predictions = [
        {
            "case_id": "cross",
            "decision": "snapshot_advance",
            "left_target": "instrument:A",
            "right_target": "instrument:A",
        },
        {"case_id": "missing", "decision": "uncertain", "left_target": None, "right_target": None},
    ]

    score = score_e6_predictions(cases, predictions)

    assert score["metrics"]["exact_target_precision"] == 0.5
    assert score["metrics"]["target_coverage"] == 0.5
    assert score["metrics"]["cross_target_supersede"] == 1
    assert score["metrics"]["missing_to_uncertain"] == 1.0
