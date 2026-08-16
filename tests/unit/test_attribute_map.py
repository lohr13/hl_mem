import pytest

from hl_mem.domain.claims.attributes import (
    ATTRIBUTE_ALLOWLIST,
    PREDICATE_ATTRIBUTE_MAP,
    canonical_conflict_slot,
    infer_canonical_attribute,
    is_mutually_exclusive_attribute,
    predicate_for_canonical_attribute,
    reconcile_canonical_attribute,
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
        ("配置", "CI 失败只允许重跑一次", "config.policy"),
        ("计划", "截止到 8 月 1 日", "plan.deadline"),
        ("事实", "当前采用 Codex", "fact.tool_choice"),
        ("配置", "hl_mem 当前版本为 v0.21.0", "config.version"),
        ("事实", "hl_mem 依赖 numpy>=2.0", "fact.dependency"),
        ("事实", "hl_mem 采用事件溯源双通道架构", "fact.architecture"),
        ("explicit_memory", "记住发布前跑测试", "memory.explicit"),
        ("unknown", "任意", "custom.unknown"),
    ],
)
def test_infer_canonical_attribute_is_table_driven(predicate, value, expected) -> None:
    assert infer_canonical_attribute(predicate, "用户", value) == expected


@pytest.mark.parametrize("value", ["importance", "import", "transport", "importing"])
def test_config_port_requires_a_complete_port_token(value: str) -> None:
    assert infer_canonical_attribute("配置", "hl_mem", value) == "config.other"


@pytest.mark.parametrize("value", ["监听 8200", "port=8200", "0.0.0.0:8200"])
def test_config_port_requires_valid_port_semantics(value: str) -> None:
    assert infer_canonical_attribute("配置", "hl_mem", value) == "config.port"


@pytest.mark.parametrize("value", ["port=0", "port=65536", "监听端口"])
def test_config_port_rejects_missing_or_out_of_range_values(value: str) -> None:
    assert infer_canonical_attribute("配置", "hl_mem", value) == "config.other"


def test_mapping_declares_only_allowlisted_attributes() -> None:
    for allowed, fallback in PREDICATE_ATTRIBUTE_MAP.values():
        assert set(allowed) <= ATTRIBUTE_ALLOWLIST
        assert fallback in ATTRIBUTE_ALLOWLIST


@pytest.mark.parametrize(
    ("legacy_attribute", "expected"),
    [
        ("版本", "config.version"),
        ("依赖", "fact.dependency"),
        ("架构", "fact.architecture"),
    ],
)
def test_chinese_attribute_aliases_close_registry_fallbacks(
    legacy_attribute: str,
    expected: str,
) -> None:
    assert (
        validate_canonical_attribute(
            predicate_for_canonical_attribute(expected, "事实"),
            legacy_attribute,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("attribute", "llm_predicate", "expected"),
    [
        ("preference.response_style", "事实", "偏好"),
        ("identity.role", "事实", "身份"),
        ("config.port", "事实", "配置"),
        ("state.process", "事实", "状态"),
        ("plan.goal", "事实", "计划"),
        ("choice.tool", "事实", "使用"),
        ("fact.capability", "配置", "事实"),
        ("memory.explicit", "事实", "explicit_memory"),
        ("custom.unknown", "计划", "计划"),
        ("invented.slot", "配置", "配置"),
    ],
)
def test_predicate_is_projected_from_registered_attribute(
    attribute: str,
    llm_predicate: str,
    expected: str,
) -> None:
    assert predicate_for_canonical_attribute(attribute, llm_predicate) == expected


def test_reconcile_accepts_registered_cross_family_attribute_without_content_conflict() -> None:
    assert reconcile_canonical_attribute(
        predicate="事实",
        llm_attribute="config.port",
        inferred_attribute="fact.other",
        subject="hl_mem",
        value="hl_mem 端口为 8200",
    ) == ("config.port", "registered_attribute")


def test_reconcile_rejects_unsubstantiated_config_port_from_model() -> None:
    assert reconcile_canonical_attribute(
        predicate="配置",
        llm_attribute="config.port",
        inferred_attribute="config.other",
        subject="hl_mem",
        value="importance 权重为 0.5",
    ) == ("config.other", "port_semantics_rejected")


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
