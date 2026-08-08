"""实体别名归一化与顶层 subject 类型守卫测试。"""

from __future__ import annotations

import pytest

from hl_mem.domain.entity import invalid_subject_reason, normalize_entity_id


@pytest.mark.parametrize(
    "alias",
    [
        "我",
        "本人",
        "我自己",
        "I",
        "Ｉ",
        "ME",
        "my",
        "myself",
        "user",
        "ＵＳＥＲ",
        "the user",
        "用户",
        "当前用户",
    ],
)
def test_persona_aliases_are_normalized(alias: str) -> None:
    """第一人称和用户标签应在 NFKC/casefold 后统一为 namespace 内的 user。"""
    assert normalize_entity_id(alias) == "user"


@pytest.mark.parametrize(
    "alias",
    ["hl_mem 项目", "hl_mem项目", "hl_mem 服务", "hl_mem_plugin", "HL-Mem"],
)
def test_hl_mem_aliases_are_normalized(alias: str) -> None:
    """benchmark 中的项目别名应统一归一化。"""
    assert normalize_entity_id(alias) == "hl_mem"


def test_unlisted_people_and_products_are_not_merged() -> None:
    assert normalize_entity_id("Alice") == "alice"
    assert normalize_entity_id("Bob") == "bob"
    assert normalize_entity_id("Alice") != normalize_entity_id("Bob")
    assert normalize_entity_id("IKEA Bookcase") == "ikea bookcase"
    assert normalize_entity_id("IKEA Bookcase") != normalize_entity_id("IKEA Desk")


@pytest.mark.parametrize(
    ("subject", "reason"),
    [
        ("server.py", "filename"),
        ("src/hl_mem/server.py", "path"),
        ("HlMemProvider", "class_name"),
        ("ALL_PROXY", "environment_variable"),
    ],
)
def test_invalid_top_level_subject_types(subject: str, reason: str) -> None:
    """技术实现标识不应成为顶层 subject。"""
    assert invalid_subject_reason(subject) == reason


@pytest.mark.parametrize("subject", ["hl_mem", "用户", "Hermes", "Codex", "Hindsight"])
def test_normal_subjects_are_accepted(subject: str) -> None:
    """正常项目、用户和产品主体不受守卫影响。"""
    assert invalid_subject_reason(subject) is None
