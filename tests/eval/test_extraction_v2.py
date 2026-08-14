"""Extraction-evaluation v2 contract and deterministic metric tests."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from hl_mem.evaluation.extraction_v2 import (
    ExtractionGoldError,
    load_extraction_gold,
    score_dedup_pairs,
    score_extraction_case,
    score_extraction_majority,
)

FIXTURE = Path(__file__).with_name("fixtures") / "extraction_v2_synthetic.json"


def _case(case_id: str):
    corpus = load_extraction_gold(FIXTURE)
    return next(case for case in corpus.cases if case.case_id == case_id)


def _faithful_recommendation() -> dict[str, object]:
    return {
        "subject": "程岚",
        "predicate": "建议",
        "value": "程岚建议周野投资星港债券",
        "entities": ["程岚", "周野", "星港债券"],
        "source_event_indices": [0],
    }


def test_synthetic_fixture_freezes_experiment_and_dedup_distribution() -> None:
    corpus = load_extraction_gold(FIXTURE)

    assert corpus.schema_version == 2
    assert corpus.data_classification == "synthetic_public"
    assert len(corpus.cases) == 24
    assert sum(case.experiment == "entities_hybrid_a" for case in corpus.cases) == 12
    assert sum(case.experiment == "proper_noun_prompt_b" for case in corpus.cases) == 12
    assert len(corpus.dedup_pairs) == 40
    assert sum(pair.expected == "reuse" for pair in corpus.dedup_pairs) == 20
    assert sum(pair.expected == "distinct" for pair in corpus.dedup_pairs) == 20
    distinct_pairs = [pair for pair in corpus.dedup_pairs if pair.expected == "distinct"]
    assert sum(bool(set(pair.left.proper_entities) & set(pair.right.proper_entities)) for pair in distinct_pairs) >= 15
    assert sum(set(pair.left.proper_entities) == set(pair.right.proper_entities) for pair in distinct_pairs) >= 5
    assert all(case.gold_units for case in corpus.cases)
    assert Counter(case.category for case in corpus.cases if case.experiment == "entities_hybrid_a") == {
        "zh_multi_person": 3,
        "reported_speech": 2,
        "place_org_boundary": 2,
        "product_project_model": 2,
        "bilingual": 2,
        "no_proper_entity_negative": 1,
    }
    assert Counter(case.category for case in corpus.cases if case.experiment == "proper_noun_prompt_b") == {
        "zh_first_person_name_binding": 4,
        "en_first_person_name_binding": 2,
        "speaker_quote_same_name": 3,
        "nickname_formal_name": 2,
        "no_explicit_name_negative": 1,
    }


def test_gold_loader_rejects_missing_atomic_unit_contract(tmp_path: Path) -> None:
    malformed = {
        "schema_version": 2,
        "data_classification": "synthetic_public",
        "cases": [
            {
                "case_id": "broken",
                "experiment": "entities_hybrid_a",
                "category": "broken",
                "events": [{"speaker": "user", "text": "synthetic"}],
                "gold_units": [
                    {
                        "unit_id": "missing-source-indices",
                        "role_action_object": {
                            "roles": [],
                            "actions": ["is"],
                            "objects": ["synthetic"],
                            "ordered_anchors": ["is", "synthetic"],
                        },
                        "proper_entities": [],
                        "speaker": "user",
                        "canonical_subject": "user",
                        "forbidden_propagation": [],
                        "modality": None,
                        "requires_self_contained_chain": False,
                    }
                ],
            }
        ],
        "dedup_pairs": [],
    }
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(malformed, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ExtractionGoldError, match="source_event_indices"):
        load_extraction_gold(path)


def test_gold_loader_rejects_out_of_range_source_event_index(tmp_path: Path) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["cases"][0]["gold_units"][0]["source_event_indices"] = [99]
    path = tmp_path / "bad-index.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ExtractionGoldError, match="source_event_indices"):
        load_extraction_gold(path)


def test_gold_loader_requires_ordered_chain_to_cover_every_role_action_and_object(tmp_path: Path) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["cases"][0]["gold_units"][0]["role_action_object"]["ordered_anchors"].remove("加入")
    path = tmp_path / "incomplete-chain.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ExtractionGoldError, match="ordered_anchors"):
        load_extraction_gold(path)


def test_relation_role_direction_requires_ordered_roles_actions_and_object() -> None:
    case = _case("a04-reported-recommendation")
    faithful = score_extraction_case(case, [_faithful_recommendation()])
    reversed_roles = score_extraction_case(
        case,
        [
            {
                **_faithful_recommendation(),
                "subject": "周野",
                "value": "周野建议程岚投资星港债券",
            }
        ],
    )

    assert faithful.relation_role_direction == 1.0
    assert reversed_roles.relation_role_direction == 0.0


def test_modality_negative_rejects_recommendation_promoted_to_execution_or_ownership() -> None:
    case = _case("a04-reported-recommendation")
    faithful = score_extraction_case(case, [_faithful_recommendation()])
    promoted = score_extraction_case(
        case,
        [
            {
                "subject": "周野",
                "predicate": "投资",
                "value": "周野已经投资并持有星港债券",
                "entities": ["周野", "星港债券"],
                "source_event_indices": [0],
            }
        ],
    )

    assert faithful.modality_negative == 1.0
    assert promoted.modality_negative == 0.0


def test_entity_coverage_scores_only_the_entities_field_against_exact_gold() -> None:
    case = _case("a04-reported-recommendation")
    partial = score_extraction_case(
        case,
        [
            {
                **_faithful_recommendation(),
                "entities": ["程岚", "周野", "债券"],
            }
        ],
    )
    surface_only = score_extraction_case(case, [{**_faithful_recommendation(), "entities": []}])

    assert partial.entity_precision == pytest.approx(2 / 3)
    assert partial.entity_recall == pytest.approx(2 / 3)
    assert partial.entity_f1 == pytest.approx(2 / 3)
    assert surface_only.entity_recall == 0.0


def test_entity_coverage_matches_multiple_units_by_anchor_coverage_not_raw_hit_count() -> None:
    case = _case("b01-zh-name-binding")
    result = score_extraction_case(
        case,
        [
            {
                "subject": "user",
                "predicate": "身份",
                "value": "我的名字叫陈默",
                "entities": ["陈默"],
                "source_event_indices": [0],
            },
            {
                "subject": "user",
                "predicate": "负责",
                "value": "陈默在星河实验室负责检索平台",
                "entities": ["陈默", "星河实验室", "检索平台"],
                "source_event_indices": [0],
            },
        ],
    )

    assert result.entity_precision == 1.0
    assert result.entity_recall == 1.0


def test_entity_coverage_keeps_case_and_whitespace_exact() -> None:
    case = _case("b05-en-name-binding")
    result = score_extraction_case(
        case,
        [
            {
                "subject": "user",
                "predicate": "maintain",
                "value": "Elena Park maintains the Comet Index at Northwind Lab",
                "entities": ["elena park", "CometIndex", "Northwind Lab"],
                "source_event_indices": [0],
            }
        ],
    )

    assert result.entity_precision == pytest.approx(1 / 3)
    assert result.entity_recall == pytest.approx(1 / 3)


def test_chain_atomicity_rejects_roles_split_across_leaf_claims() -> None:
    case = _case("a04-reported-recommendation")
    split = score_extraction_case(
        case,
        [
            {
                "subject": "程岚",
                "predicate": "建议",
                "value": "程岚向周野提出建议",
                "entities": ["程岚", "周野"],
                "source_event_indices": [0],
            },
            {
                "subject": "周野",
                "predicate": "投资",
                "value": "投资星港债券",
                "entities": ["周野", "星港债券"],
                "source_event_indices": [0],
            },
        ],
    )

    assert split.chain_atomicity == 0.0
    assert score_extraction_case(case, [_faithful_recommendation()]).chain_atomicity == 1.0


def test_forbidden_propagation_is_scoped_by_source_event() -> None:
    case = _case("a04-reported-recommendation")
    leaked = {
        **_faithful_recommendation(),
        "value": "程岚建议周野投资星港债券；周野还没有投资",
    }

    assert score_extraction_case(case, [_faithful_recommendation()]).forbidden_propagation == 1.0
    assert score_extraction_case(case, [leaked]).forbidden_propagation == 0.0


def test_multisample_stability_uses_strict_majority() -> None:
    case = _case("a04-reported-recommendation")
    bad = [{**_faithful_recommendation(), "value": "周野建议程岚投资星港债券"}]
    result = score_extraction_majority(
        case,
        [[_faithful_recommendation()], bad, [_faithful_recommendation()]],
    )

    assert result.sample_count == 3
    assert result.majority["relation_role_direction"] == 1.0
    assert result.support["relation_role_direction"] == pytest.approx(2 / 3)


def test_multisample_requires_an_odd_number_of_at_least_three_samples() -> None:
    case = _case("a04-reported-recommendation")

    with pytest.raises(ValueError, match="odd number"):
        score_extraction_majority(case, [[_faithful_recommendation()]] * 2)


def test_scoring_requires_explicit_prediction_source_indices() -> None:
    case = _case("a04-reported-recommendation")
    claim = _faithful_recommendation()
    del claim["source_event_indices"]

    with pytest.raises(ValueError, match="source_event_indices"):
        score_extraction_case(case, [claim])


def test_dedup_pair_scoring_measures_false_reuse_separately() -> None:
    corpus = load_extraction_gold(FIXTURE)
    decisions = {pair.pair_id: pair.expected for pair in corpus.dedup_pairs}
    decisions["distinct-01"] = "reuse"

    result = score_dedup_pairs(corpus.dedup_pairs, decisions)

    assert result.total == 40
    assert result.correct == 39
    assert result.false_reuse_count == 1
    assert result.reuse_precision == pytest.approx(20 / 21)
    assert result.reuse_recall == 1.0
    assert result.distinct_recall == pytest.approx(19 / 20)
    assert result.accuracy == pytest.approx(39 / 40)


def test_dedup_pair_scoring_requires_one_decision_per_pair() -> None:
    corpus = load_extraction_gold(FIXTURE)

    with pytest.raises(ValueError, match="missing decisions"):
        score_dedup_pairs(corpus.dedup_pairs, {})
