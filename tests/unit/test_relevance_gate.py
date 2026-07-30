"""relevance gate observe 阶段的配置与只读诊断测试。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

import pytest

import hl_mem.application.recall as recall_module
from hl_mem.application.recall import RecallService
from hl_mem.domain.recall import RecallIntent
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.relevance import (
    enforce_relevance,
    evaluate_relevance,
    should_enforce_relevance,
)
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer
from hl_mem.settings import Settings


def _tracer() -> SearchTracer:
    """构造不包含正文的测试 trace。"""
    return SearchTracer(
        SearchTrace(
            query_id="query-1",
            query_hash="hash",
            intent="current_state",
            limit=3,
            candidate_limit=10,
            candidates={},
            phases=SearchPhaseMetrics(),
        )
    )


def test_off_mode_keeps_trace_without_relevance_diagnostics() -> None:
    """默认 off 不执行评估，保持旧 trace 形态的空诊断值。"""
    settings = Settings()
    tracer = _tracer()
    tracer.record_channel("fts", [{"id": "claim-1"}])

    assert settings.relevance_gate_mode == "off"
    assert "relevance_decision" not in tracer.to_dict()["candidates"]["claim-1"]
    assert tracer.to_dict()["candidates"]["claim-1"]["filter_reasons"] == []


def test_observe_mode_does_not_truncate_or_reorder_results() -> None:
    """observe 只写诊断，不修改传入结果的顺序和内容。"""
    tracer = _tracer()
    claims = [{"id": "claim-1"}, {"id": "claim-2"}, {"id": "claim-3"}]
    tracer.record_channel("fts", claims)
    tracer.record_channel("dense", [{**claim, "_score": 0.2} for claim in claims])
    before = [dict(claim) for claim in claims]

    evaluate_relevance(
        [str(claim["id"]) for claim in claims],
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
    )

    assert claims == before
    assert [claim["id"] for claim in claims] == ["claim-1", "claim-2", "claim-3"]
    assert all(candidate["relevance_decision"] == "irrelevant" for candidate in tracer.to_dict()["candidates"].values())


def test_reranker_applied_uses_only_raw_reranker_floor() -> None:
    """applied 路径使用 reranker raw score，并标记阈值附近候选。"""
    tracer = _tracer()
    tracer.record_channel("fts", [{"id": "high"}, {"id": "near"}, {"id": "low"}])
    tracer.record_rerank([("high", 0.8), ("near", 0.38), ("low", 0.1)])

    decisions = evaluate_relevance(
        ["high", "near", "low"],
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
    )

    assert decisions["high"].decision == "relevant"
    assert decisions["near"].decision == "borderline"
    assert decisions["low"].decision == "irrelevant"
    assert "below_reranker_floor" in tracer.to_dict()["candidates"]["low"]["relevance_reasons"]
    assert tracer.to_dict()["candidates"]["low"]["filter_reasons"] == []


def test_reranker_fallback_uses_channel_evidence() -> None:
    """fallback 路径要求包含 dense 的多通道候选仍达到 dense floor。"""
    tracer = _tracer()
    tracer.record_channel("fts", [{"id": "combined"}, {"id": "multi"}, {"id": "fts-tag"}, {"id": "weak"}])
    tracer.record_channel(
        "dense",
        [
            {"id": "combined", "_score": 0.35},
            {"id": "multi", "_score": 0.1},
            {"id": "weak", "_score": 0.1},
        ],
    )
    tracer.record_channel("tag", [{"id": "multi"}, {"id": "fts-tag"}])

    decisions = evaluate_relevance(
        ["combined", "multi", "fts-tag", "weak"],
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
    )

    assert decisions["combined"].decision == "relevant"
    assert decisions["multi"].decision == "irrelevant"
    assert decisions["fts-tag"].decision == "relevant"
    assert decisions["weak"].decision == "irrelevant"


def test_observe_records_relative_drop_for_every_adjacent_candidate() -> None:
    """observe 为全部同路径相邻候选记录相对降幅，并统一使用包含阈值。"""
    tracer = _tracer()
    claims = [{"id": "top"}, {"id": "middle"}, {"id": "tail"}]
    tracer.record_channel("fts", claims)
    tracer.record_rerank([("top", 1.0), ("middle", 0.8), ("tail", 0.4)])

    decisions = evaluate_relevance(
        [str(claim["id"]) for claim in claims],
        tracer,
        reranker_floor=0.3,
        dense_floor=0.3,
        relative_drop_threshold=0.5,
    )

    assert decisions["middle"].relative_drop == pytest.approx(0.2)
    assert decisions["tail"].relative_drop == pytest.approx(0.5)
    assert "relative_score_drop" in tracer.to_dict()["candidates"]["tail"]["relevance_reasons"]


def test_fallback_fts_only_candidate_is_irrelevant_without_dense_evidence() -> None:
    """仅 FTS 命中且无 dense 证据时记录 below_dense_floor。"""
    tracer = _tracer()
    tracer.record_channel("fts", [{"id": "fts-only"}])

    decisions = evaluate_relevance(
        ["fts-only"],
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
    )

    assert decisions["fts-only"].decision == "irrelevant"
    assert tracer.to_dict()["candidates"]["fts-only"]["relevance_reasons"] == [
        "below_dense_floor",
        "query_no_evidence",
    ]
    assert tracer.to_dict()["candidates"]["fts-only"]["filter_reasons"] == []


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("relevance_reranker_floor", -0.01),
        ("relevance_dense_floor", 1.01),
        ("relevance_relative_drop", 2.0),
    ],
)
def test_relevance_thresholds_must_be_unit_interval(field_name: str, value: float) -> None:
    """relevance 阈值必须处于闭区间 [0, 1]。"""
    with pytest.raises(ConfigurationError):
        replace(Settings(), **{field_name: value}).validate()


def test_relevance_settings_snapshot_contains_non_sensitive_values() -> None:
    """健康快照公开 relevance 配置但不涉及密钥。"""
    snapshot = Settings().snapshot()

    assert snapshot["relevance_gate_mode"] == "off"
    assert snapshot["relevance_reranker_floor"] == 0.4
    assert snapshot["relevance_dense_floor"] == 0.3
    assert snapshot["relevance_relative_drop"] == 0.15
    assert snapshot["relevance_keep_top1"] is True
    assert snapshot["relevance_intents"] == ["current_state"]


def test_relevance_settings_are_explicitly_configurable() -> None:
    """统一配置可显式开启 observe 并覆盖全部阈值。"""
    settings = Settings(
        relevance_gate_mode="observe",
        relevance_reranker_floor=0.45,
        relevance_dense_floor=0.35,
        relevance_relative_drop=0.2,
        relevance_keep_top1=False,
        relevance_intents=("current_state", "preference"),
    )

    assert settings.relevance_gate_mode == "observe"
    assert settings.relevance_reranker_floor == 0.45
    assert settings.relevance_dense_floor == 0.35
    assert settings.relevance_relative_drop == 0.2
    assert settings.relevance_keep_top1 is False
    assert settings.relevance_intents == ("current_state", "preference")


def test_enforce_truncates_low_score_tail_contiguously() -> None:
    """enforce 从首个低相关候选起丢弃整个尾部。"""
    tracer = _tracer()
    claims = [{"id": "top"}, {"id": "low"}, {"id": "later-high"}]
    tracer.record_channel("fts", claims)
    tracer.record_rerank([("top", 0.8), ("low", 0.2), ("later-high", 0.9)])

    retained = enforce_relevance(
        claims,
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=1.0,
        keep_top1=True,
    )

    assert [item["id"] for item in retained] == ["top"]
    assert tracer.to_dict()["candidates"]["later-high"]["included"] is False


def test_enforce_keep_top1_retains_low_score_candidate_with_basic_signal() -> None:
    """keep_top1 保留存在评分信号的低分首项。"""
    tracer = _tracer()
    claims = [{"id": "top"}]
    tracer.record_channel("fts", claims)
    tracer.record_rerank([("top", 0.1)])

    retained = enforce_relevance(
        claims,
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
        keep_top1=True,
    )

    assert retained == claims


def test_enforce_without_keep_top1_can_return_no_evidence() -> None:
    """关闭 keep_top1 后首项也应用门槛并可返回空结果。"""
    tracer = _tracer()
    claims = [{"id": "top"}]
    tracer.record_channel("fts", claims)
    tracer.record_rerank([("top", 0.1)])

    retained = enforce_relevance(
        claims,
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
        keep_top1=False,
    )

    assert retained == []
    assert tracer.to_dict()["candidates"]["top"]["included"] is False


def test_enforce_truncates_at_relative_score_drop() -> None:
    """相邻证据分数断层超过阈值时截断当前项及后续。"""
    tracer = _tracer()
    claims = [{"id": "top"}, {"id": "drop"}, {"id": "tail"}]
    tracer.record_channel("fts", claims)
    tracer.record_rerank([("top", 0.9), ("drop", 0.6), ("tail", 0.59)])

    retained = enforce_relevance(
        claims,
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.2,
        keep_top1=True,
    )

    assert [item["id"] for item in retained] == ["top"]
    assert "relative_score_drop" in tracer.to_dict()["candidates"]["drop"]["filter_reasons"]


def test_enforce_truncates_when_relative_drop_equals_threshold() -> None:
    """相邻降幅恰好等于阈值时也执行截断。"""
    tracer = _tracer()
    claims = [{"id": "top"}, {"id": "drop"}]
    tracer.record_channel("fts", claims)
    tracer.record_rerank([("top", 1.0), ("drop", 0.5)])

    retained = enforce_relevance(
        claims,
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.5,
        keep_top1=True,
    )

    assert [item["id"] for item in retained] == ["top"]


def test_enforce_top1_without_basic_signal_returns_empty() -> None:
    """Top-1 没有任何通道或 reranker 证据时，即使 keep_top1 也不保留。"""
    tracer = _tracer()

    retained = enforce_relevance(
        [{"id": "top"}],
        tracer,
        reranker_floor=0.4,
        dense_floor=0.3,
        relative_drop_threshold=0.15,
        keep_top1=True,
    )

    assert retained == []


def test_enforce_only_applies_to_intent_allowlist() -> None:
    """enforce 默认只截断 current_state，preference 仍保持 observe 行为。"""
    allowed_intents = Settings().relevance_intents

    assert should_enforce_relevance("enforce", "current_state", allowed_intents) is True
    assert should_enforce_relevance("enforce", "preference", allowed_intents) is False
    assert should_enforce_relevance("observe", "current_state", allowed_intents) is False
    assert should_enforce_relevance("off", "current_state", allowed_intents) is False


@pytest.mark.parametrize(
    ("intent", "expected_ids"),
    [
        (RecallIntent.CURRENT_STATE, ["top"]),
        (RecallIntent.PREFERENCE, ["top", "low"]),
    ],
)
def test_recall_service_side_effects_use_final_enforced_results(
    monkeypatch: pytest.MonkeyPatch,
    intent: RecallIntent,
    expected_ids: list[str],
) -> None:
    """服务只为截断后的结果记录 access/exposure，非白名单 intent 不截断。"""
    claims = [{"id": "top"}, {"id": "low"}]

    def fake_hybrid_claims(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        tracer = kwargs["tracer"]
        tracer.record_channel("fts", claims)
        tracer.record_rerank([("top", 0.8), ("low", 0.1)])
        tracer.record_final(claims)
        return [dict(claim) for claim in claims]

    monkeypatch.setattr(recall_module, "hybrid_claims", fake_hybrid_claims)
    monkeypatch.setattr(recall_module.ExperienceService, "list_policies", lambda *args, **kwargs: [])
    service = RecallService(
        sqlite3.connect(":memory:"),
        FakeEmbedder(4),
        settings=replace(Settings(), relevance_gate_mode="enforce"),
    )
    accessed: list[str] = []
    exposed: list[str] = []
    monkeypatch.setattr(
        service,
        "_record_access",
        lambda items: accessed.extend(str(item["id"]) for item in items),
    )
    monkeypatch.setattr(service, "_assemble_results", lambda items, namespace: [dict(item) for item in items])
    monkeypatch.setattr(service, "_assemble_observations", lambda claim_ids: [])
    monkeypatch.setattr(
        service,
        "_record_feedback",
        lambda items, observations, policies, query_id: exposed.extend(str(item["id"]) for item in items),
    )

    response = service.recall("query", intent=intent)

    assert [item["id"] for item in response["results"]] == expected_ids
    assert accessed == expected_ids
    assert exposed == expected_ids
    assert response["total"] == len(response["results"])
