from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

import tests.eval.relation_chain_holdout as holdout

MANIFEST_PATH = Path(__file__).parent / "fixtures" / "relation_chain_holdout_manifest.json"
V2_MANIFEST_PATH = Path(__file__).parent / "fixtures" / "relation_chain_holdout_v2_manifest.json"
EXPECTED_DISTRIBUTION = {
    "recommendation_execution": 4,
    "reporting_ownership": 4,
    "enumeration_completeness": 4,
    "cross_event_two_hop": 4,
    "conflict_latest_value": 4,
    "no_answer_trap": 4,
}


def test_manifest_seals_questions_and_freezes_distribution_and_hash() -> None:
    manifest = holdout.load_holdout_manifest(MANIFEST_PATH)

    assert manifest.schema_version == 1
    assert manifest.dataset_id == "zh-relation-chain-holdout-v1"
    assert manifest.case_count == 24
    assert manifest.category_counts == EXPECTED_DISTRIBUTION
    assert manifest.gold_schema_version == 3
    assert manifest.scorer_version == "answer-entity-packet-v1"
    assert manifest.access_policy == "sealed_final_preregistered_validation_only"
    assert len(manifest.sha256) == 64
    assert not manifest.source_path.is_absolute()

    raw_manifest = MANIFEST_PATH.read_text(encoding="utf-8")
    assert '"cases"' not in raw_manifest
    assert '"question"' not in raw_manifest
    assert '"answer_entities"' not in raw_manifest


def test_loader_refuses_accidental_holdout_access() -> None:
    with pytest.raises(holdout.SealedHoldoutAccessError, match="allow_sealed=True"):
        holdout.load_sealed_holdout(MANIFEST_PATH)


def test_installed_sealed_holdout_has_24_valid_cases_and_same_gold_contract() -> None:
    manifest = holdout.load_holdout_manifest(MANIFEST_PATH)
    if not holdout.resolve_holdout_path(manifest).is_file():
        pytest.skip("sealed relation-chain holdout is not installed")

    dataset = holdout.load_sealed_holdout(MANIFEST_PATH, allow_sealed=True)

    assert dataset.dataset_id == manifest.dataset_id
    assert len(dataset.cases) == 24
    assert Counter(case.category for case in dataset.cases) == Counter(EXPECTED_DISTRIBUTION)
    assert len({case.case_id for case in dataset.cases}) == 24
    assert all(case.case_id.startswith("rc-holdout-v1-") for case in dataset.cases)
    assert all(case.events and case.question and case.answer for case in dataset.cases)

    no_answer_cases = [case for case in dataset.cases if case.gold.answerability == "no_answer"]
    answerable_cases = [case for case in dataset.cases if case.gold.answerability == "answerable"]
    assert len(no_answer_cases) == 4
    assert len(answerable_cases) == 20
    assert all(case.gold.answer_entities is None for case in no_answer_cases)
    assert all(case.gold.forbidden_entities or case.gold.forbidden_assertions for case in no_answer_cases)
    assert all(case.gold.answer_entities for case in answerable_cases)


def test_hash_mismatch_is_rejected_before_cases_are_exposed(tmp_path: Path) -> None:
    payload = tmp_path / "holdout.json"
    payload.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        """{
  "schema_version": 1,
  "dataset_id": "zh-relation-chain-holdout-v1",
  "source_path": "holdout.json",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "case_count": 24,
  "category_counts": {
    "recommendation_execution": 4,
    "reporting_ownership": 4,
    "enumeration_completeness": 4,
    "cross_event_two_hop": 4,
    "conflict_latest_value": 4,
    "no_answer_trap": 4
  },
  "gold_schema_version": 3,
  "scorer_version": "answer-entity-packet-v1",
  "access_policy": "sealed_final_preregistered_validation_only"
}
""",
        encoding="utf-8",
    )

    with pytest.raises(holdout.HoldoutHashMismatch, match="SHA-256"):
        holdout.load_sealed_holdout(manifest_path, allow_sealed=True)


def test_v2_manifest_and_explicit_relation_coverage_are_loadable(tmp_path: Path) -> None:
    cases = []
    categories = tuple(EXPECTED_DISTRIBUTION)
    for index in range(24):
        category = categories[index // 4]
        case_id = f"rc-holdout-v2-{index + 1:03d}"
        no_answer = category == "no_answer_trap"
        gold = {
            "answerability": "no_answer" if no_answer else "answerable",
            "role_action_object": [],
            "forbidden_entities": ["禁答实体"] if no_answer else [],
            "forbidden_assertions": [],
        }
        if not no_answer:
            gold["answer_entities"] = [f"实体{index + 1}"]
        cases.append(
            {
                "case_id": case_id,
                "category": category,
                "namespace": f"eval:sealed:v2:{index + 1:03d}",
                "events": [
                    {
                        "event_id": f"v2-{index + 1:03d}-e1",
                        "occurred_at": "2026-01-01T00:00:00+08:00",
                        "text": f"全新测试事件{index + 1}",
                    }
                ],
                "question_at": "2026-01-02T00:00:00+08:00",
                "question": f"全新测试问题{index + 1}？",
                "answer": "无法确定" if no_answer else f"实体{index + 1}",
                "gold": gold,
                "provenance": "synthetic_loader_contract_test",
                "relation_coverage": "none" if no_answer else "required",
            }
        )
    payload = {
        "schema_version": 1,
        "dataset_id": "zh-relation-chain-holdout-v2",
        "cases": cases,
    }
    payload_path = tmp_path / "holdout-v2.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    manifest_path = tmp_path / "manifest-v2.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "zh-relation-chain-holdout-v2",
                "source_path": payload_path.name,
                "sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest(),
                "case_count": 24,
                "category_counts": EXPECTED_DISTRIBUTION,
                "gold_schema_version": 3,
                "scorer_version": "answer-entity-packet-v1",
                "access_policy": "sealed_final_preregistered_validation_only",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dataset = holdout.load_sealed_holdout(manifest_path, allow_sealed=True)

    assert dataset.dataset_id == "zh-relation-chain-holdout-v2"
    assert all(case.case_id.startswith("rc-holdout-v2-") for case in dataset.cases)
    assert Counter(case.relation_coverage for case in dataset.cases) == {"required": 20, "none": 4}


def test_v2_manifest_freezes_new_external_payload() -> None:
    manifest = holdout.load_holdout_manifest(V2_MANIFEST_PATH)

    assert manifest.suite_version == "v2"
    assert manifest.dataset_id == "zh-relation-chain-holdout-v2"
    assert manifest.case_prefix == "rc-holdout-v2-"
    assert manifest.case_count == 24
    assert manifest.category_counts == EXPECTED_DISTRIBUTION
    assert not manifest.source_path.is_absolute()


def test_installed_v2_is_balanced_and_does_not_reuse_v1_text() -> None:
    v1_manifest = holdout.load_holdout_manifest(MANIFEST_PATH)
    v2_manifest = holdout.load_holdout_manifest(V2_MANIFEST_PATH)
    if (
        not holdout.resolve_holdout_path(v1_manifest).is_file()
        or not holdout.resolve_holdout_path(v2_manifest).is_file()
    ):
        pytest.skip("both sealed holdout payloads are required for cross-version validation")

    v1 = holdout.load_sealed_holdout(MANIFEST_PATH, allow_sealed=True)
    v2 = holdout.load_sealed_holdout(V2_MANIFEST_PATH, allow_sealed=True)
    v1_text = {case.question for case in v1.cases}
    v1_text.update(event.text for case in v1.cases for event in case.events)
    v2_text = {case.question for case in v2.cases}
    v2_text.update(event.text for case in v2.cases for event in case.events)

    assert len(v2.cases) == 24
    assert Counter(case.category for case in v2.cases) == Counter(EXPECTED_DISTRIBUTION)
    assert Counter(case.relation_coverage for case in v2.cases) == {"required": 20, "none": 4}
    assert not (v1_text & v2_text)
