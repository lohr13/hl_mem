"""查询标签确定性解析测试。"""

from hl_mem.domain.claims.query_tags import extract_query_tags


def test_extract_query_tags_maps_chinese_and_english_in_stable_order() -> None:
    """中英文命中应去重并保持首次出现顺序。"""
    assert extract_query_tags("架构 architecture 决策和 migration 设计") == [
        "architecture",
        "decision",
        "migration",
    ]


def test_extract_query_tags_ignores_low_information_and_partial_english_words() -> None:
    """低信息标签和英文子串不应触发检索加权。"""
    assert extract_query_tags("preference fact configuration planner") == []


def test_extract_query_tags_returns_empty_for_unrecognized_query() -> None:
    """没有可识别标签时应返回空列表。"""
    assert extract_query_tags("今天天气怎么样") == []
