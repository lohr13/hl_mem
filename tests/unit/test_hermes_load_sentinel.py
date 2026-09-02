"""Hermes 插件加载期哨兵的回归测试。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

from hl_mem import __version__
from hl_mem.adapters.hermes.deployment import PLUGIN_SOURCE_DIR


def _load_plugin(module_name: str) -> ModuleType:
    source = PLUGIN_SOURCE_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_load_appends_load_attempt_with_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    _load_plugin("_hermes_sentinel_normal")

    [line] = (state_dir / "hl_mem-load.log").read_text(encoding="utf-8").splitlines()
    fields = dict(field.split("=", 1) for field in line.split()[1:])
    assert line.startswith("load-attempt ")
    assert datetime.fromisoformat(fields["timestamp"]).tzinfo is not None
    assert fields["pid"] == str(os.getpid())
    assert fields["hl_mem_version"] == __version__


def test_load_sentinel_resolves_environment_and_platform_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugin = _load_plugin("_hermes_sentinel_resolution")

    assert plugin._find_hermes_home_for_load_attempt() == tmp_path.resolve()

    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setattr(plugin.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(plugin.sys, "platform", "linux")
    assert plugin._find_hermes_home_for_load_attempt() == (tmp_path / ".hermes").resolve()

    local_appdata = tmp_path / "local-appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setattr(plugin.sys, "platform", "win32")
    assert plugin._find_hermes_home_for_load_attempt() == (local_appdata / "hermes").resolve()


def test_load_log_write_failure_does_not_block_plugin_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_home = tmp_path / "missing"
    monkeypatch.setenv("HERMES_HOME", str(missing_home))

    plugin = _load_plugin("_hermes_sentinel_unwritable")

    assert plugin.create_provider is not None
    assert not (missing_home / "state" / "hl_mem-load.log").exists()


def test_load_attempt_survives_hl_mem_import_failure(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "state").mkdir(parents=True)
    source = PLUGIN_SOURCE_DIR / "__init__.py"
    script = """
import builtins
import runpy
import sys

real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "hl_mem" or name.startswith("hl_mem."):
        raise ImportError("blocked hl_mem import")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
try:
    runpy.run_path(sys.argv[1], run_name="_broken_hermes_plugin")
except ImportError:
    pass
"""
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(hermes_home)

    completed = subprocess.run(
        [sys.executable, "-c", script, str(source)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    line = (hermes_home / "state" / "hl_mem-load.log").read_text(encoding="utf-8").strip()
    assert line.startswith("load-attempt ")
    assert "hl_mem_version=<import-failed>" in line


def test_clean_process_load_attempt_records_imported_version(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "state").mkdir(parents=True)
    source = PLUGIN_SOURCE_DIR / "__init__.py"
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(hermes_home)
    environment["PYTHONPATH"] = str(PLUGIN_SOURCE_DIR.parents[3])

    completed = subprocess.run(
        [sys.executable, "-c", "import runpy,sys; runpy.run_path(sys.argv[1])", str(source)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    line = (hermes_home / "state" / "hl_mem-load.log").read_text(encoding="utf-8").strip()
    assert f"hl_mem_version={__version__}" in line
