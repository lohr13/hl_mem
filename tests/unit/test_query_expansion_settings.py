"""Query expansion model selection tests."""

import pytest

from hl_mem.components import make_query_expander
from hl_mem.errors import ConfigurationError
from hl_mem.llm.providers import DashScopeProvider, ZhipuProvider
from hl_mem.settings import Settings


def test_query_expander_uses_dedicated_model() -> None:
    settings = Settings(
        llm_api_key="test-key",
        llm_model="qwen3.7-plus",
        query_expansion_mode="auto",
        query_expansion_model="glm-4.7",
    )

    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.model == "glm-4.7"


@pytest.mark.parametrize("override", [None, "", "   "])
def test_query_expander_uses_main_model_when_override_is_empty(override: str | None) -> None:
    settings = Settings(
        llm_api_key="test-key",
        llm_model="qwen3.7-plus",
        query_expansion_mode="auto",
        query_expansion_model=override,
    )

    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.model == "qwen3.7-plus"


def test_query_expander_inherits_main_line_when_dedicated_line_is_unset() -> None:
    settings = Settings(
        llm_api_key="main-secret",
        llm_base_url="https://main.example.com/v1",
        llm_model="main-model",
        llm_provider="zhipu",
        llm_reasoning_effort="low",
        query_expansion_mode="auto",
        query_expansion_model="qe-model",
    )

    settings.validate()
    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.api_key == "main-secret"
    assert expander.client.base_url == "https://main.example.com/v1"
    assert expander.client.model == "qe-model"
    assert isinstance(expander.client.provider, ZhipuProvider)
    assert expander.client.provider.reasoning_effort == "low"


def test_query_expander_uses_complete_dedicated_dashscope_line() -> None:
    settings = Settings(
        llm_api_key=None,
        llm_provider="zhipu",
        query_expansion_mode="auto",
        query_expansion_model="qe-model",
        query_expansion_provider="dashscope",
        query_expansion_base_url="https://qe.example.com/v1",
        query_expansion_api_key="qe-secret",
        enable_llm_thinking=True,
    )

    settings.validate()
    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.api_key == "qe-secret"
    assert expander.client.base_url == "https://qe.example.com/v1"
    assert expander.client.model == "qe-model"
    assert isinstance(expander.client.provider, DashScopeProvider)
    assert expander.client.provider.enable_thinking is True


def test_dedicated_zhipu_query_expander_inherits_reasoning_effort() -> None:
    settings = Settings(
        query_expansion_mode="auto",
        query_expansion_provider="zhipu",
        query_expansion_base_url="https://qe.example.com/v1",
        query_expansion_api_key="qe-secret",
        llm_reasoning_effort="high",
    )

    settings.validate()
    expander = make_query_expander(settings)

    assert expander is not None
    assert isinstance(expander.client.provider, ZhipuProvider)
    assert expander.client.provider.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        (
            {
                "query_expansion_base_url": "https://qe.example.com/v1",
                "query_expansion_api_key": "qe-secret",
            },
            "recall.query_expansion_provider",
        ),
        (
            {
                "query_expansion_provider": "dashscope",
                "query_expansion_api_key": "qe-secret",
            },
            "recall.query_expansion_base_url",
        ),
        (
            {
                "query_expansion_provider": "dashscope",
                "query_expansion_base_url": "https://qe.example.com/v1",
            },
            "QUERY_EXPANSION_API_KEY",
        ),
        (
            {
                "query_expansion_provider": "unsupported",
                "query_expansion_base_url": "https://qe.example.com/v1",
                "query_expansion_api_key": "qe-secret",
            },
            "recall.query_expansion_provider",
        ),
    ],
)
def test_dedicated_query_expansion_line_requires_complete_valid_triplet(
    overrides: dict[str, str],
    expected_message: str,
) -> None:
    settings = Settings(query_expansion_mode="auto", **overrides)

    with pytest.raises(ConfigurationError, match=expected_message):
        make_query_expander(settings)
