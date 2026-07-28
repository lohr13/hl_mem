"""LLM 提取边界的顶层 subject 守卫测试。"""

from hl_mem.ingest.llm_extractor import LLMExtractor


def test_extractor_replaces_class_name_subject_with_valid_entity() -> None:
    """类名不能成为顶层 subject，应优先使用 claim 内的有效实体。"""
    claim = LLMExtractor._claim(
        {
            "subject": "HlMemProvider",
            "predicate": "配置",
            "value": "HlMemProvider 通过环境变量配置",
            "entities": ["HlMemProvider", "hl_mem"],
        }
    )

    assert claim.subject == "hl_mem"
    assert claim.entities == ["HlMemProvider", "hl_mem"]


def test_extractor_downgrades_class_name_without_valid_entity() -> None:
    """没有有效替代实体时使用候选占位，同时保留原始类名。"""
    claim = LLMExtractor._claim(
        {
            "subject": "HlMemProvider",
            "predicate": "事实",
            "value": "HlMemProvider 是一个类",
        }
    )

    assert claim.subject == "unknown_subject"
    assert claim.entities == ["HlMemProvider"]
