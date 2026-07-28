"""Behavioral memory scenarios — 独立可执行的记忆行为测试层。"""

from pathlib import Path

import pytest


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Module | None:
    """收集保留历史文件名的中文行为场景测试模块。"""
    if file_path.name == "chinese_test_cases.py":
        return pytest.Module.from_parent(parent, path=file_path)
    return None
