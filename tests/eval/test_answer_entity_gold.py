from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

import tests.eval.chinese_e2e as chinese_e2e

SAMPLE_MANIFEST_PATH = Path(__file__).parent / "fixtures" / "chinese_e2e_sample.json"


def _gold(*entities: str) -> object:
    return chinese_e2e.AnswerEntityGold(
        answerability="answerable",
        answer_entities=entities,
        role_action_object=(),
        forbidden_entities=(),
        forbidden_assertions=(),
    )


def test_v3_manifest_freezes_answer_entity_gold_for_all_forty_cases() -> None:
    manifest = chinese_e2e.load_sample_manifest(SAMPLE_MANIFEST_PATH)

    assert manifest.schema_version == 3
    assert manifest.answer_entity_scorer_version == "answer-entity-packet-v1"
    assert len(manifest.answer_entity_gold_by_case_id) == 40
    assert set(manifest.answer_entity_gold_by_case_id) == {
        case_id for case_ids in manifest.expected_case_ids.values() for case_id in case_ids
    }
    assert manifest.accepted_rubrics_by_question_hash

    for gold in manifest.answer_entity_gold_by_case_id.values():
        assert gold.answerability == "answerable"
        assert gold.answer_entities
        for value in (
            *gold.answer_entities,
            *gold.forbidden_entities,
            *gold.forbidden_assertions,
        ):
            assert value == unicodedata.normalize("NFC", value)


def test_entity_coverage_at_five_counts_final_packet_from_top_five_seeds() -> None:
    gold = _gold("高盛", "华尔街金融集团")
    packet = [
        {"seed_rank": 2, "entities": ["高盛"]},
        {
            "seed_rank": None,
            "expanded_from_seed_ranks": [4],
            "entities": ["华尔街金融集团"],
        },
        {"seed_rank": 6, "entities": ["不应计入"]},
    ]

    scored = chinese_e2e.score_answer_entity_packet(packet, gold, answer_text="", k=5)

    assert scored["entity_coverage_at_5"] == 1.0
    assert scored["covered_entities"] == ["高盛", "华尔街金融集团"]
    assert scored["missing_entities"] == []
    assert scored["scorer_version"] == "answer-entity-packet-v1"


def test_entity_coverage_is_partial_and_nfc_exact_without_synonym_expansion() -> None:
    gold = _gold("高盛", "Café")
    packet = [
        {"seed_rank": 1, "entities": ["Goldman Sachs"]},
        {"seed_rank": 3, "entities": ["Cafe\u0301"]},
    ]

    scored = chinese_e2e.score_answer_entity_packet(packet, gold, answer_text="", k=5)

    assert scored["entity_coverage_at_5"] == 0.5
    assert scored["covered_entities"] == ["Café"]
    assert scored["missing_entities"] == ["高盛"]


def test_forbidden_entities_and_assertions_are_hard_negative_violations() -> None:
    gold = chinese_e2e.AnswerEntityGold(
        answerability="answerable",
        answer_entities=("高盛",),
        role_action_object=(chinese_e2e.RoleActionObject(role="李磊", action="推荐", object="高盛债券"),),
        forbidden_entities=("摩根大通",),
        forbidden_assertions=("李明已经购买高盛债券",),
    )

    scored = chinese_e2e.score_answer_entity_packet(
        [{"seed_rank": 1, "entities": ["高盛", "摩根大通"]}],
        gold,
        answer_text="李明已经购买高盛债券。",
        k=5,
    )

    assert scored["forbidden_entity_hits"] == ["摩根大通"]
    assert scored["forbidden_assertion_hits"] == ["李明已经购买高盛债券"]
    assert scored["negative_violation"] is True


def test_no_answer_gold_has_no_entities_and_is_excluded_from_entity_coverage() -> None:
    gold = chinese_e2e.AnswerEntityGold(
        answerability="no_answer",
        answer_entities=None,
        role_action_object=(),
        forbidden_entities=("上海",),
        forbidden_assertions=("会议在上海举行",),
    )

    scored = chinese_e2e.score_answer_entity_packet(
        [{"seed_rank": 1, "entities": ["上海"]}],
        gold,
        answer_text="会议在上海举行。",
        k=5,
    )
    aggregate = chinese_e2e.aggregate_answer_entity_scores(
        [
            chinese_e2e.score_answer_entity_packet(
                [{"seed_rank": 1, "entities": ["高盛"]}],
                _gold("高盛", "华尔街金融集团"),
                answer_text="",
                k=5,
            ),
            chinese_e2e.score_answer_entity_packet(
                [{"seed_rank": 1, "entities": ["杭州"]}],
                _gold("杭州"),
                answer_text="",
                k=5,
            ),
            scored,
        ]
    )

    assert scored["entity_coverage_at_5"] is None
    assert scored["negative_violation"] is True
    assert aggregate["entity_coverage_at_5"] == pytest.approx(0.75)
    assert aggregate["entity_coverage_cases"] == 2
    assert aggregate["no_answer_cases"] == 1
    assert aggregate["negative_violation_rate"] == pytest.approx(1 / 3)
