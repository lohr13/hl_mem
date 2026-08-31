"""LLM 思考模式配置与组件传递测试。"""

from dataclasses import replace

import pytest

from hl_mem.components import make_llm_client
from hl_mem.errors import ConfigurationError
from hl_mem.llm.providers import DashScopeProvider, OpenAICompatibleProvider, ZhipuProvider
from hl_mem.llm.types import LLMMessage, LLMRequest, StructuredOutputMode
from hl_mem.settings import Settings


def test_llm_thinking_defaults_to_disabled() -> None:
    assert Settings().enable_llm_thinking is False
    assert Settings().llm_thinking_control == "auto"


def test_llm_thinking_can_be_enabled() -> None:
    assert Settings(enable_llm_thinking=True).enable_llm_thinking is True


def test_llm_thinking_rejects_invalid_value() -> None:
    with pytest.raises(ConfigurationError, match=r"llm\.enable_thinking"):
        replace(Settings.for_test(), enable_llm_thinking="sometimes").validate()  # type: ignore[arg-type]


def test_llm_thinking_control_rejects_invalid_value() -> None:
    with pytest.raises(ConfigurationError, match=r"llm\.thinking_control"):
        replace(Settings.for_test(), llm_thinking_control="top_level").validate()  # type: ignore[arg-type]


def test_llm_thinking_is_exposed_in_health_snapshot() -> None:
    snapshot = Settings(
        enable_llm_thinking=True,
        llm_thinking_control="chat_template_kwargs",
    ).snapshot()

    assert snapshot["enable_llm_thinking"] is True
    assert snapshot["llm_thinking_control"] == "chat_template_kwargs"


def test_make_llm_client_passes_thinking_setting_to_dashscope(tmp_path) -> None:
    client = make_llm_client(
        Settings(
            database_path=str(tmp_path / "memory.db"),
            llm_api_key="test-key",
            llm_provider="dashscope",
            enable_llm_thinking=True,
        )
    )

    assert isinstance(client.provider, DashScopeProvider)
    assert client.provider.enable_thinking is True
    client.close()


def test_make_llm_client_keeps_non_dashscope_provider_constructor_compatible(tmp_path) -> None:
    client = make_llm_client(
        Settings(
            database_path=str(tmp_path / "memory.db"),
            llm_api_key="test-key",
            llm_provider="zhipu",
            llm_thinking_control="chat_template_kwargs",
        )
    )

    assert isinstance(client.provider, ZhipuProvider)
    payload = client.provider.build_payload(
        "model",
        LLMRequest(messages=[LLMMessage(role="user", content="extract")]),
        StructuredOutputMode.JSON_OBJECT,
    )
    assert "chat_template_kwargs" not in payload
    client.close()


def test_make_llm_client_passes_chat_template_thinking_control_to_openai_compatible(tmp_path) -> None:
    client = make_llm_client(
        Settings(
            database_path=str(tmp_path / "memory.db"),
            llm_api_key="test-key",
            llm_provider="openai_compatible",
            enable_llm_thinking=True,
            llm_thinking_control="chat_template_kwargs",
        )
    )

    assert isinstance(client.provider, OpenAICompatibleProvider)
    payload = client.provider.build_payload(
        "model",
        LLMRequest(messages=[LLMMessage(role="user", content="extract")]),
        StructuredOutputMode.JSON_OBJECT,
    )
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}
    client.close()
