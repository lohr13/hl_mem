"""召回管线上下文测试。"""

from hl_mem.recall.staged_pipeline import RecallContext


def test_recall_context_initializes_stage_results_independently() -> None:
    """RecallContext 应为每次召回创建独立的阶段结果容器。"""
    first = RecallContext(repo=object())
    second = RecallContext(repo=object())

    first.query_tags.append("preference")

    assert first.query_tags == ["preference"]
    assert second.query_tags == []
