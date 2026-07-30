"""LLM 思考模式配置与组件传递测试。"""

import pytest

from hl_mem.components import make_llm_client
from hl_mem.errors import ConfigurationError
from hl_mem.llm.providers import DashScopeProvider, ZhipuProvider
from hl_mem.settings import Settings


def test_llm_thinking_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("HL_MEM_LLM_ENABLE_THINKING", raising=False)

    assert Settings.from_env().enable_llm_thinking is False


def test_llm_thinking_can_be_enabled_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_LLM_ENABLE_THINKING", "true")

    assert Settings.from_env().enable_llm_thinking is True


def test_llm_thinking_rejects_invalid_environment_value(monkeypatch) -> None:
    monkeypatch.setenv("HL_MEM_LLM_ENABLE_THINKING", "sometimes")

    with pytest.raises(ConfigurationError, match="HL_MEM_LLM_ENABLE_THINKING"):
        Settings.from_env()


def test_llm_thinking_is_exposed_in_health_snapshot() -> None:
    assert Settings(enable_llm_thinking=True).snapshot()["enable_llm_thinking"] is True


def test_make_llm_client_passes_thinking_setting_to_dashscope() -> None:
    client = make_llm_client(
        Settings(
            llm_api_key="test-key",
            llm_provider="dashscope",
            enable_llm_thinking=True,
        )
    )

    assert isinstance(client.provider, DashScopeProvider)
    assert client.provider.enable_thinking is True


def test_make_llm_client_keeps_non_dashscope_provider_constructor_compatible() -> None:
    client = make_llm_client(Settings(llm_api_key="test-key", llm_provider="zhipu"))

    assert isinstance(client.provider, ZhipuProvider)
