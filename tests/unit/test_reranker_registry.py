"""Reranker provider registry tests."""

from __future__ import annotations

from hl_mem.recall.reranker import DashScopeReranker, FakeReranker, make_reranker
from hl_mem.settings import Settings


def test_make_reranker_off_returns_none() -> None:
    assert make_reranker(Settings(reranker_mode="off")) is None


def test_make_reranker_fake() -> None:
    assert isinstance(make_reranker(Settings(reranker_mode="fake")), FakeReranker)


def test_make_reranker_real_dashscope() -> None:
    reranker = make_reranker(
        Settings(
            reranker_mode="real",
            reranker_provider="dashscope",
            reranker_api_key="test-key",
        )
    )

    assert isinstance(reranker, DashScopeReranker)


def test_reranker_provider_config() -> None:
    assert Settings(reranker_provider="dashscope").reranker_provider == "dashscope"
