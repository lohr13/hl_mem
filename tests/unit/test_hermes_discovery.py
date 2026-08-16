"""Hermes 根目录探测的单元测试。"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path


def test_find_hermes_home_ignores_unmarked_agent_subdirectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """只有虚拟环境的 hermes-agent 子目录不能遮蔽真正的 Hermes 根目录。"""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "plugins").mkdir(parents=True)
    (hermes_home / "hermes-agent" / ".venv").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    discovery = importlib.import_module("hl_mem.adapters.hermes.discovery")

    assert discovery.find_hermes_home(None) == hermes_home.resolve()


def test_discovery_imports_without_project_or_third_party_dependencies() -> None:
    """隔离解释器只提供 src 时也必须能导入 discovery。"""
    project_src = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_src)

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from hl_mem.adapters.hermes.discovery import find_hermes_home; print(find_hermes_home.__name__)",
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "find_hermes_home"
