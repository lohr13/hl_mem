"""查询标签确定性解析测试。"""

import pytest

from hl_mem.domain.claims.query_tags import extract_query_slot_hints, extract_query_tags


def test_query_slot_hints_cover_high_precision_patterns() -> None:
    assert extract_query_slot_hints("我叫什么名字")[0] == ["identity.name"]
    assert extract_query_slot_hints("GPU 型号")[0] == ["config.hardware"]
    assert extract_query_slot_hints("embedding 模型")[0] == ["config.model"]
    assert extract_query_slot_hints("REDACTED_PROXY 代理")[0] == ["config.network"]
    assert extract_query_slot_hints("我喜欢什么工具")[0] == ["preference.*"]


@pytest.mark.parametrize(
    "query",
    [
        "我的 GPU 是什么",
        "显卡型号",
        "显存有多少",
        "how much VRAM",
        "which graphics card do I use",
        "本机处理器是哪款",
        "which processor is installed",
    ],
)
def test_hardware_aliases_map_to_config_hardware(query: str) -> None:
    """常见中英文硬件问法应产生同一个 operational slot hint。"""
    assert extract_query_slot_hints(query)[0] == ["config.hardware"]


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
