"""Query expansion model selection tests."""

import pytest

from hl_mem.components import make_query_expander
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
