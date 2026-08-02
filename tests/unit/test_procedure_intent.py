"""Tool/Procedure 专用召回的领域与 packing 回归测试。"""

from __future__ import annotations

from hl_mem.application.recall import budget_pack_by_type
from hl_mem.domain.recall import route_recall_intent
from hl_mem.domain.temporal import RecallIntent
from hl_mem.recall.procedure_pipeline import MemoryCandidate
from hl_mem.settings import Settings


def test_routes_tool_and_procedure_signals_with_expected_priority() -> None:
    """强信号、部署组合及偏好/历史优先级必须保持确定性。"""
    assert route_recall_intent("部署工具有哪些", None) is RecallIntent.TOOL
    assert route_recall_intent("which tool should I use", None) is RecallIntent.TOOL
    assert route_recall_intent("这个服务怎么部署", None) is RecallIntent.PROCEDURE
    assert route_recall_intent("how to configure it", None) is RecallIntent.PROCEDURE
    assert route_recall_intent("我上次怎么排障的", None) is RecallIntent.PROCEDURE
    assert route_recall_intent("我偏好哪个工具", None) is RecallIntent.PREFERENCE
    assert route_recall_intent("历史上用过哪个工具", None) is RecallIntent.HISTORICAL


def test_settings_default_to_keyword_and_validate_bounds() -> None:
    """默认路由模式必须是 keyword，且候选与时间窗口有界。"""
    settings = Settings.for_test()
    assert settings.procedure_recall_mode == "keyword"
    assert settings.procedure_candidate_limit == 30
    settings.validate()


def test_type_quota_packing_reflows_without_exceeding_budget() -> None:
    """不足配额须回流，且估算 token 总量不能超过预算。"""
    candidates = [
        MemoryCandidate("policy", "p1", "abcd", 1.0, (), {}),
        MemoryCandidate("episode", "e1", "efgh", 1.0, (), {}),
        MemoryCandidate("episode", "e2", "ijkl", 0.9, (), {}),
        MemoryCandidate("claim", "c1", "mnop", 1.0, (), {}),
    ]
    packed, quotas, reflow = budget_pack_by_type(candidates, RecallIntent.TOOL, 8)
    assert sum(max(1, (len(item.text) + 1) // 2) for item in packed) <= 8
    assert quotas == {"policy": 2, "episode": 2, "trace": 1, "claim": 2}
    assert reflow >= 1
    assert {item.memory_type for item in packed} == {"policy", "episode", "claim"}
