"""Pytest 全局配置。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator

import pytest

from hl_mem.storage.database import Database
from tests.support.sqlite_ownership import TestSQLiteOwner

os.environ.setdefault("HL_MEM_ENV", "test")
os.environ.setdefault("HL_MEM_EXTRACTOR", "fake")
os.environ.setdefault("HL_MEM_EMBEDDER", "fake")
os.environ.setdefault("HL_MEM_RERANKER", "off")


@pytest.fixture(autouse=True)
def disable_optional_llm_features(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭非专项测试中的可选 LLM 功能和召回折叠。"""
    monkeypatch.setenv("HL_MEM_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setenv("HL_MEM_RELATION_DISCOVERY_MODE", "off")
    monkeypatch.setenv("HL_MEM_RECALL_DEDUP_THRESHOLD", "0.0")


@pytest.fixture(autouse=True)
def sqlite_test_owner(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestSQLiteOwner]:
    """Close every SQLite resource created during an ordinary test."""
    owner = TestSQLiteOwner()
    if request.node.get_closest_marker("no_sqlite_autoclose") is None:
        owner.install(monkeypatch)
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture
def database_factory(sqlite_test_owner: TestSQLiteOwner) -> Callable[..., Database]:
    """Create a Database owned by the current test."""
    return sqlite_test_owner.database


@pytest.fixture
def sqlite_connection_factory(sqlite_test_owner: TestSQLiteOwner) -> Callable[..., sqlite3.Connection]:
    """Create a raw SQLite connection owned by the current test."""
    return sqlite_test_owner.connect


def pytest_configure(config: pytest.Config) -> None:
    """注册测试标记。"""
    config.addinivalue_line("markers", "real_api: requires real API keys")
    config.addinivalue_line(
        "markers",
        "no_sqlite_autoclose: disable test-harness SQLite cleanup for lifecycle tests",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """默认跳过真实 API 测试，显式使用对应 marker 时允许执行。"""
    if "real_api" in config.option.markexpr:
        return
    skip_real = pytest.mark.skip(reason="real_api tests skipped (set -m real_api to run)")
    for item in items:
        if "real_api" in item.keywords:
            item.add_marker(skip_real)
