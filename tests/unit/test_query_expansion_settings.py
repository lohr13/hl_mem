"""Query expansion model selection tests."""

import pytest

from hl_mem.components import make_query_expander
from hl_mem.errors import ConfigurationError
from hl_mem.llm.providers import DashScopeProvider, ZhipuProvider
from hl_mem.settings import Settings

_PARKED_DEDICATED_LINES = (
    {
        "query_expansion_base_url": "https://qe.example.com/v1",
        "query_expansion_api_key": "qe-secret",
    },
    {
        "query_expansion_provider": "dashscope",
        "query_expansion_api_key": "qe-secret",
    },
    {
        "query_expansion_provider": "dashscope",
        "query_expansion_base_url": "https://qe.example.com/v1",
    },
    {
        "query_expansion_provider": "unsupported",
        "query_expansion_base_url": "https://qe.example.com/v1",
        "query_expansion_api_key": "qe-secret",
    },
)
_INCOMPLETE_DEDICATED_LINES = _PARKED_DEDICATED_LINES[:3]


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "extractor_mode": "real",
        "embedder_mode": "real",
        "reranker_mode": "off",
        "llm_api_key": "llm-secret",
        "embedding_api_key": "embedding-secret",
        "query_expansion_mode": "off",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("overrides", _PARKED_DEDICATED_LINES)
def test_disabled_query_expansion_ignores_parked_dedicated_line(
    overrides: dict[str, str],
) -> None:
    settings = _production_settings(**overrides)

    settings.validate()
    settings.validate_runtime()

    assert make_query_expander(settings) is None


@pytest.mark.parametrize("overrides", _INCOMPLETE_DEDICATED_LINES)
def test_active_query_expansion_rejects_invalid_dedicated_line(
    overrides: dict[str, str],
) -> None:
    settings = _production_settings(query_expansion_mode="auto", **overrides)

    with pytest.raises(ConfigurationError):
        settings.validate()


def test_zero_query_expansion_limit_does_not_resolve_parked_line() -> None:
    settings = Settings(
        query_expansion_mode="auto",
        query_expansion_max=0,
        query_expansion_provider="dashscope",
    )

    assert make_query_expander(settings) is None


def test_query_expander_uses_dedicated_model(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
        llm_api_key="test-key",
        llm_model="qwen3.7-plus",
        query_expansion_mode="auto",
        query_expansion_model="glm-4.7",
    )

    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.model == "glm-4.7"
    expander.client.close()


@pytest.mark.parametrize("override", [None, "", "   "])
def test_query_expander_uses_main_model_when_override_is_empty(tmp_path, override: str | None) -> None:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
        llm_api_key="test-key",
        llm_model="qwen3.7-plus",
        query_expansion_mode="auto",
        query_expansion_model=override,
    )

    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.model == "qwen3.7-plus"
    expander.client.close()


def test_query_expander_inherits_main_line_when_dedicated_line_is_unset(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
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
    expander.client.close()


def test_query_expander_inherits_main_line_when_only_dedicated_api_key_is_set(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
        llm_api_key="main-secret",
        llm_base_url="https://main.example.com/v1",
        llm_provider="zhipu",
        query_expansion_mode="auto",
        query_expansion_api_key="parked-secret",
    )

    settings.validate()
    expander = make_query_expander(settings)

    assert expander is not None
    assert expander.client.api_key == "main-secret"
    assert expander.client.base_url == "https://main.example.com/v1"
    assert isinstance(expander.client.provider, ZhipuProvider)
    expander.client.close()


def test_query_expander_uses_complete_dedicated_dashscope_line(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
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
    expander.client.close()


def test_dedicated_zhipu_query_expander_inherits_reasoning_effort(tmp_path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "memory.db"),
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
    expander.client.close()


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
