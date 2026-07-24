import pytest

from hl_mem.domain.claims.attributes import (
    ATTRIBUTE_ALLOWLIST,
    PREDICATE_ATTRIBUTE_MAP,
    canonical_conflict_slot,
    infer_canonical_attribute,
    is_mutually_exclusive_attribute,
    validate_canonical_attribute,
)


@pytest.mark.parametrize(
    ("predicate", "value", "expected"),
    [
        ("偏好", "我喜欢深色模式", "preference.ui_theme"),
        ("使用", "PostgreSQL", "choice.database"),
        ("状态", "测试全部通过", "state.test_suite"),
        ("身份", "开发者", "identity.role"),
        ("配置", "端口 10808", "config.port"),
        ("计划", "截止到 8 月 1 日", "plan.deadline"),
        ("事实", "当前采用 Codex", "fact.tool_choice"),
        ("explicit_memory", "记住发布前跑测试", "memory.explicit"),
        ("unknown", "任意", "custom.unknown"),
    ],
)
def test_infer_canonical_attribute_is_table_driven(predicate, value, expected) -> None:
    assert infer_canonical_attribute(predicate, "用户", value) == expected


def test_mapping_declares_only_allowlisted_attributes() -> None:
    for allowed, fallback in PREDICATE_ATTRIBUTE_MAP.values():
        assert set(allowed) <= ATTRIBUTE_ALLOWLIST
        assert fallback in ATTRIBUTE_ALLOWLIST


def test_attribute_validation_rejects_unknown_or_wrong_predicate_attribute() -> None:
    assert validate_canonical_attribute("偏好", "preference.tool_choice") == "preference.tool_choice"
    assert validate_canonical_attribute("偏好", "config.port") == "preference.other"
    assert validate_canonical_attribute("偏好", "invented.slot") == "custom.unknown"
    assert validate_canonical_attribute("unknown", "invented.slot") == "custom.unknown"


@pytest.mark.parametrize(
    ("attribute", "slot"),
    [
        ("preference.tool_choice", "preference.tool_choice"),
        ("choice.tool", "choice.tool"),
        ("fact.tool_choice", "fact.tool_choice"),
        ("choice.database", "choice.database"),
        ("config.port", "config.port"),
        ("config.network", "config.network"),
        ("config.path", "config.path"),
        ("config.env", "config.env"),
        ("invented.slot", "custom.unknown"),
    ],
)
def test_canonical_conflict_slot_aliases(attribute, slot) -> None:
    assert canonical_conflict_slot(attribute) == slot


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        ("preference.ui_theme", True),
        ("preference.response_style", True),
        ("config.port", True),
        ("config.model", True),
        ("state.service_health", True),
        ("plan.deadline", False),
        ("choice.tool", False),
        ("config.env", False),
        ("custom.unknown", False),
        (None, False),
    ],
)
def test_is_mutually_exclusive_attribute(attribute, expected) -> None:
    assert is_mutually_exclusive_attribute(attribute) is expected
