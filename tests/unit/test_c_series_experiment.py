"""C-series 冻结实验语义的纯函数测试。"""

from __future__ import annotations

import json

import pytest

from hl_mem.evaluation.c_series import (
    ARM_IDS,
    PREREGISTRATION_REQUIRED_FIELDS,
    arm_order,
    arm_spec,
    atomic_pack,
    build_preregistration,
    case_seed,
    evidence_sufficiency_v1,
    is_retryable_error,
    parse_planner_output,
    planner_prompt,
    relation_multihop_intent_v1,
    rescue_mode,
    select_raw_events,
    validate_preregistration,
)
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


def test_arm_specs_freeze_switches_depth_and_budgets() -> None:
    assert ARM_IDS == ("C0", "C1", "C2", "C3", "C4", "C5", "f4")
    c0 = arm_spec("C0")
    assert (c0.relation_enabled, c0.max_depth, c0.relation_candidate_limit) == (False, 0, 0)
    assert (c0.raw_fallback, c0.planner) == (False, False)
    assert (arm_spec("C1").intent_gated, arm_spec("C1").max_depth) == (False, 1)
    assert (arm_spec("C2").intent_gated, arm_spec("C2").relation_candidate_limit) == (True, 12)
    assert (arm_spec("C3").max_depth, arm_spec("C3").relation_candidate_limit) == (2, 20)
    assert arm_spec("C4").atomic_path_packing is True
    assert (arm_spec("C5").raw_fallback, arm_spec("C5").planner) == (True, False)
    assert (arm_spec("f4").raw_fallback, arm_spec("f4").planner) == (False, True)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("项目负责人的常驻城市是哪里？", True),
        ("完整列出所有参加发布会的人。", True),
        ("王经理推荐的方案最后执行了吗？", True),
        ("现在使用的数据库版本是什么？", True),
        ("我今天喝了什么？", False),
        ("请总结这段会议记录。", False),
    ],
)
def test_relation_multihop_intent_v1(query: str, expected: bool) -> None:
    assert relation_multihop_intent_v1(query).eligible is expected


def test_evidence_sufficiency_v1_uses_frozen_weighted_observed_formula() -> None:
    sufficient = evidence_sufficiency_v1(
        answerability="supported",
        required_rao=("role", "action", "object"),
        covered_rao=("role", "action", "object"),
        query_entities=("甲", "乙"),
        packet_entities=("甲", "乙"),
    )
    assert sufficient.score == pytest.approx(1.0)
    assert sufficient.insufficient is False

    boundary = evidence_sufficiency_v1(
        answerability="supported",
        required_rao=("role", "action", "object"),
        covered_rao=("role", "action"),
        query_entities=("甲",),
        packet_entities=(),
    )
    assert boundary.score == pytest.approx(0.6833333333333333)
    assert boundary.insufficient is True

    low_confidence = evidence_sufficiency_v1(
        answerability="low_confidence",
        required_rao=("role", "action", "object"),
        covered_rao=("role",),
        query_entities=(),
        packet_entities=(),
    )
    assert low_confidence.insufficient is True
    assert "low_confidence_rao" in low_confidence.reasons


def test_atomic_pack_keeps_a_relation_path_whole_or_falls_back_to_rank_order() -> None:
    candidates = [
        {"claim_id": "seed", "token_count": 50, "rank": 1},
        {"claim_id": "bridge", "token_count": 120, "rank": 7},
        {"claim_id": "answer", "token_count": 140, "rank": 8},
        {"claim_id": "other", "token_count": 60, "rank": 2},
    ]
    packed = atomic_pack(
        candidates,
        [{"claim_ids": ["seed", "bridge", "answer"], "expansion_score": 0.8}],
    )
    assert [item["claim_id"] for item in packed.items][:3] == ["seed", "bridge", "answer"]
    assert packed.atomic_path_claim_ids == ("seed", "bridge", "answer")

    too_large = [dict(item, token_count=401) for item in candidates]
    fallback = atomic_pack(
        too_large,
        [{"claim_ids": ["seed", "bridge", "answer"], "expansion_score": 0.8}],
    )
    assert fallback.atomic_path_claim_ids == ()
    assert [item["claim_id"] for item in fallback.items] == ["seed", "other", "bridge", "answer"]


def test_rescue_modes_share_gate_and_are_mutually_exclusive() -> None:
    assert rescue_mode("C5", intent=True, insufficient=True) == "raw"
    assert rescue_mode("f4", intent=True, insufficient=True) == "planner"
    assert rescue_mode("C5", intent=False, insufficient=True) is None
    assert rescue_mode("f4", intent=True, insufficient=False) is None
    assert rescue_mode("C4", intent=True, insufficient=True) is None


def test_planner_contract_is_bounded_and_rejects_free_form() -> None:
    prompt = planner_prompt(
        "项目负责人的常驻城市是哪里？",
        [{"claim_id": "seed", "entities": ["项目"], "slot": "fact"}],
        [{"from_id": "seed", "to_id": "answer", "relation": "supports"}],
    )
    assert len(prompt) <= 2_400
    assert parse_planner_output('{"subgoals":[{"query":"负责人是谁","max_depth":1}]}') == (
        {"query": "负责人是谁", "max_depth": 1},
    )
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_planner_output("负责人是甲")


def test_raw_fallback_filters_namespace_and_bitemporal_window(tmp_path) -> None:
    database = Database(tmp_path / "raw.db")
    try:
        with database.connect() as connection:
            repository = EventRepository(connection)
            for event_id, tenant, occurred, recorded in (
                ("linked", "wanted", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
                ("other", "other", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
                ("future", "wanted", "2027-01-01T00:00:00+00:00", "2027-01-02T00:00:00+00:00"),
            ):
                repository.insert_event(
                    {
                        "id": event_id,
                        "tenant_id": tenant,
                        "event_type": "message",
                        "actor_type": "user",
                        "content": {"text": f"alpha {event_id}"},
                        "occurred_at": occurred,
                        "recorded_at": recorded,
                    }
                )
            selected = select_raw_events(
                connection,
                query="alpha",
                namespace="wanted",
                question_at="2026-06-01T00:00:00+00:00",
                known_as_of="2026-06-01T00:00:00+00:00",
                linked_event_ids=("linked", "other", "future"),
            )
    finally:
        database.close()
    assert [item["event_id"] for item in selected] == ["linked"]


def test_seed_and_arm_order_are_deterministic_and_complete() -> None:
    first = case_seed("prereg-1", "abc", "case-1", 2)
    second = case_seed("prereg-1", "abc", "case-1", 2)
    assert first == second
    assert 0 <= first < 2**64
    assert set(arm_order(first)) == set(ARM_IDS)
    assert arm_order(first) == arm_order(second)


def test_retry_classification_only_accepts_429_and_timeouts() -> None:
    class HttpError(RuntimeError):
        status_code = 429

    class BadRequest(RuntimeError):
        status_code = 400

    assert is_retryable_error(TimeoutError()) is True
    assert is_retryable_error(HttpError()) is True
    assert is_retryable_error(BadRequest()) is False


def test_preregistration_requires_concrete_hashes_and_clean_snapshot(tmp_path) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"cases": ["one"]}), encoding="utf-8")
    cache = tmp_path / "cache.sqlite"
    cache.write_bytes(b"cache")
    manifest = build_preregistration(
        preregistration_id="c-series-test",
        git_commit="a" * 40,
        clean_source=True,
        corpus_paths={"design": corpus},
        cache_paths=[cache],
        model_snapshot={"reader": {"provider": "dashscope", "model": "qwen3.7-plus"}},
        prompt_hashes={"qa": "b" * 64, "planner": "c" * 64},
        case_ids=["one"],
    )
    assert not (PREREGISTRATION_REQUIRED_FIELDS - manifest.keys())
    validate_preregistration(manifest)
    broken = dict(manifest, clean_source=False)
    with pytest.raises(ValueError, match="clean_source"):
        validate_preregistration(broken)
