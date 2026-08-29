"""Hermes 插件在宿主进程中的配置定位与失败诊断回归测试。"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hl_mem import __version__
from hl_mem.adapters.hermes.deployment import deploy_plugin
from hl_mem.adapters.hermes.provider import HLMemProvider
from hl_mem.errors import ConfigurationError


class _Collector:
    def __init__(self) -> None:
        self.provider: Any = None

    def register_memory_provider(self, provider: Any) -> None:
        self.provider = provider


def _load_deployed_plugin(hermes_home: Path) -> ModuleType:
    """按 Hermes 的文件加载边界执行已部署插件副本。"""
    __import__("hl_mem.cli")
    __import__("hl_mem.mcp.server")
    target = deploy_plugin("upgrade", hermes_home).target_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location("_hermes_user_memory.hl_mem", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_loads_config_from_hermes_home_when_cwd_is_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切换宿主 CWD 不得让插件读取其中的同名配置。"""
    hermes_home = tmp_path / "hermes-home"
    source_tree = tmp_path / "hermes-source"
    hermes_home.mkdir()
    source_tree.mkdir()
    (hermes_home / "hl_mem.toml").write_text(
        '[hermes]\nenabled = true\n[recall]\nquery_expansion_mode = "off"\n',
        encoding="utf-8",
    )
    (source_tree / "hl_mem.toml").write_text("[unknown]\nvalue = 1\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(source_tree)
    plugin = _load_deployed_plugin(hermes_home)

    provider = plugin.create_provider()

    assert isinstance(provider, HLMemProvider)
    assert provider.settings.hermes_enabled is True


def test_explicit_plugin_config_and_env_paths_take_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方显式路径必须覆盖 HERMES_HOME 与宿主 CWD 的同名文件。"""
    hermes_home = tmp_path / "hermes-home"
    source_tree = tmp_path / "hermes-source"
    explicit_dir = tmp_path / "explicit"
    hermes_home.mkdir()
    source_tree.mkdir()
    explicit_dir.mkdir()
    (hermes_home / "hl_mem.toml").write_text("[unknown]\nvalue = 1\n", encoding="utf-8")
    (source_tree / "hl_mem.toml").write_text("[unknown]\nvalue = 2\n", encoding="utf-8")
    config_path = explicit_dir / "hl_mem.toml"
    config_path.write_text('[extraction]\nmode = "real"\n[recall]\nquery_expansion_mode = "off"\n', encoding="utf-8")
    env_path = explicit_dir / ".env"
    env_path.write_text("LLM_API_KEY=explicit-key\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.chdir(source_tree)
    plugin = _load_deployed_plugin(hermes_home)

    provider = plugin.create_provider(config_path=config_path, env_path=env_path)

    assert provider.settings.extractor_mode == "real"
    assert provider.settings.llm_api_key == "explicit-key"


def test_missing_hermes_config_logs_safe_diagnostics_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺失 canonical 配置必须留下 traceback，且不得注册伪成功实例或泄露密钥。"""
    hermes_home = tmp_path / "hermes-home"
    source_tree = tmp_path / "hermes-source"
    hermes_home.mkdir()
    source_tree.mkdir()
    secret = "sk-registration-secret"
    (hermes_home / ".env").write_text(f"LLM_API_KEY={secret}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(source_tree)
    collector = _Collector()
    plugin = _load_deployed_plugin(hermes_home)

    with caplog.at_level(logging.ERROR), pytest.raises(ConfigurationError) as caught:
        plugin.register(collector)

    expected_config = str((hermes_home / "hl_mem.toml").resolve())
    assert expected_config in str(caught.value)
    assert collector.provider is None
    assert "Hermes provider registration failed" in caplog.text
    assert "ConfigurationError" in caplog.text
    assert expected_config in caplog.text
    assert str(source_tree.resolve()) in caplog.text
    assert str(hermes_home.resolve()) in caplog.text
    assert f"hl_mem_version={__version__}" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
    assert secret not in caplog.text


def test_config_discovery_failure_is_logged_without_masking_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hermes Home 探测本身失败时也必须记录并原样抛出根因。"""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    plugin = _load_deployed_plugin(hermes_home)

    def fail_discovery(_override: Any) -> Path:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(plugin, "find_hermes_home", fail_discovery)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="discovery failed"):
        plugin.register(_Collector())

    assert "Hermes provider registration failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "config_path=<unresolved>" in caplog.text
    assert "hermes_home=<unresolved>" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
