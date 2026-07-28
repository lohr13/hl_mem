"""P1-1：可选 LLM 组件工厂的异常边界回归测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hl_mem import components
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings


@pytest.mark.parametrize(
    "factory_name", ("make_query_expander", "make_relation_discoverer")
)
def test_configuration_error_degrades_only_when_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch,
    factory_name: str,
) -> None:
    """开发环境显式启用 fake fallback 时才允许配置错误降级。"""
    settings = replace(
        Settings(),
        allow_fake_fallback=True,
        query_expansion_mode="always",
        relation_discovery_mode="audit",
    )

    def fail_configuration(*args: object, **kwargs: object) -> object:
        raise ConfigurationError("missing test key")

    monkeypatch.setattr(components, "make_llm_client", fail_configuration)
    factory = getattr(components, factory_name)

    assert factory(settings) is None
    health = components.component_health()[factory_name.removeprefix("make_")]
    assert health == {
        "requested_mode": "always"
        if factory_name == "make_query_expander"
        else "audit",
        "effective_mode": "off",
        "degradation_reason": "missing test key",
    }


@pytest.mark.parametrize("error_type", (RuntimeError, ImportError))
def test_unexpected_component_initialization_errors_propagate(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """编程错误和导入错误不得伪装成功能关闭。"""
    settings = replace(
        Settings(), query_expansion_mode="always", allow_fake_fallback=True
    )

    def fail_unexpected(*args: object, **kwargs: object) -> object:
        raise error_type("unexpected")

    monkeypatch.setattr(components, "make_llm_client", fail_unexpected)
    with pytest.raises(error_type, match="unexpected"):
        components.make_query_expander(settings)


def test_production_configuration_error_never_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production 即使误配 fallback 也必须启动失败。"""
    settings = replace(
        Settings(),
        environment="production",
        allow_fake_fallback=True,
        query_expansion_mode="always",
    )

    def fail_configuration(*args: object, **kwargs: object) -> object:
        raise ConfigurationError("missing production key")

    monkeypatch.setattr(components, "make_llm_client", fail_configuration)
    with pytest.raises(ConfigurationError, match="missing production key"):
        components.make_query_expander(settings)
