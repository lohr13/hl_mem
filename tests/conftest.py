"""Pytest 全局配置。"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """注册需要真实外部 API 的测试标记。"""
    config.addinivalue_line("markers", "real_api: requires real API keys")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """默认跳过真实 API 测试，显式使用对应 marker 时允许执行。"""
    if "real_api" in config.option.markexpr:
        return
    skip_real = pytest.mark.skip(reason="real_api tests skipped (set -m real_api to run)")
    for item in items:
        if "real_api" in item.keywords:
            item.add_marker(skip_real)
