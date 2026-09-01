"""Hermes 根目录探测的单元测试。"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_find_hermes_home_does_not_prefer_agent_source_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """完整源码 checkout 也不能遮蔽承载用户插件的 Hermes 根目录。"""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "plugins").mkdir(parents=True)
    agent_checkout = hermes_home / "hermes-agent"
    agent_checkout.mkdir(parents=True)
    (agent_checkout / "pyproject.toml").touch()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    discovery = importlib.import_module("hl_mem.adapters.hermes.discovery")

    assert discovery.find_hermes_home(None) == hermes_home.resolve()


def test_find_hermes_home_ignores_venv_only_agent_subdirectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """只有虚拟环境的 agent 子目录同样不能改变 Hermes 用户数据根。"""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "plugins").mkdir(parents=True)
    (hermes_home / "hermes-agent" / ".venv").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    discovery = importlib.import_module("hl_mem.adapters.hermes.discovery")

    assert discovery.find_hermes_home(None) == hermes_home.resolve()


def test_find_hermes_home_uses_windows_platform_default(tmp_path: Path, monkeypatch) -> None:
    """未设置环境变量时应与 Hermes 的 Windows 平台默认根保持一致。"""
    local_appdata = tmp_path / "LocalAppData"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))

    discovery = importlib.import_module("hl_mem.adapters.hermes.discovery")
    monkeypatch.setattr(sys, "platform", "win32")

    assert discovery.find_hermes_home(None) == (local_appdata / "hermes").resolve()


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


def test_plugin_registration_records_loaded_runtime_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = importlib.import_module("hl_mem.adapters.hermes.plugin")
    records: list[tuple[Path, str, str | None]] = []

    class Context:
        def register_memory_provider(self, provider: object) -> None:
            assert provider is sentinel

    sentinel = object()
    monkeypatch.setattr(plugin, "_resolve_config_paths", lambda *_args: (tmp_path / "hl_mem.toml", tmp_path / ".env"))
    monkeypatch.setattr(plugin, "create_provider", lambda **_kwargs: sentinel)
    monkeypatch.setattr(
        plugin,
        "write_runtime_status",
        lambda home, _identity, *, status, exception_type=None: records.append((Path(home), status, exception_type)),
    )

    plugin.register(Context())

    assert records == [(tmp_path, "registered", None)]


def test_plugin_registration_preserves_original_failure_when_status_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = importlib.import_module("hl_mem.adapters.hermes.plugin")

    class RegistrationFailure(RuntimeError):
        pass

    class Context:
        def register_memory_provider(self, _provider: object) -> None:
            raise RegistrationFailure("original registration failure")

    monkeypatch.setattr(plugin, "_resolve_config_paths", lambda *_args: (tmp_path / "hl_mem.toml", tmp_path / ".env"))
    monkeypatch.setattr(plugin, "create_provider", lambda **_kwargs: object())

    def fail_status(*_args: object, **_kwargs: object) -> None:
        raise OSError("status write failure")

    monkeypatch.setattr(plugin, "write_runtime_status", fail_status)

    with pytest.raises(RegistrationFailure, match="original registration failure"):
        plugin.register(Context())
