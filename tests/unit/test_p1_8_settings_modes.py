"""P1-8：Settings 模式类型和安全测试入口测试。"""

from __future__ import annotations

from typing import Literal, get_type_hints

from hl_mem.settings import Settings


def test_new_feature_modes_use_literal_annotations() -> None:
    """新增模式字段退化为裸 str 时类型检查无法阻止非法值。"""
    hints = get_type_hints(Settings)
    assert hints["query_expansion_mode"] == Literal["off", "auto", "always"]
    assert hints["procedure_recall_mode"] == Literal["off", "keyword", "auto"]
    assert hints["feedback_lifecycle_mode"] == Literal["off", "observe", "on"]
    assert hints["image_describer_mode"] == Literal["off", "on"]
    assert hints["image_describer_provider"] == Literal["dashscope"]


def test_for_test_returns_safe_non_network_configuration() -> None:
    """测试默认配置必须关闭所有会创建真实网络客户端的可选组件。"""
    settings = Settings.for_test()
    settings.validate()

    assert settings.environment == "test"
    assert settings.embedder_mode == "fake"
    assert settings.extractor_mode == "fake"
    assert settings.reranker_mode == "off"
    assert settings.query_expansion_mode == "off"
    assert settings.relation_discovery_mode == "off"
    assert settings.image_describer_mode == "off"
