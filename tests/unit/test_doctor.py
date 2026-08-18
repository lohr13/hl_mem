"""doctor 诊断命令的单元测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import hl_mem.doctor as doctor_module
from hl_mem.compatibility import (
    CONTEXT_PACKET_SCHEMA_MAJOR,
    CONTEXT_PACKET_SCHEMA_MINOR,
    DAEMON_CONTRACT_MAJOR,
    HERMES_PLUGIN_CONTRACT_MAJOR,
)
from hl_mem.doctor import (
    CheckResult,
    CheckStatus,
    DaemonProbe,
    _check_daemon_compatibility,
    _check_hermes,
    _check_plugin_compatibility,
    _check_wire_compatibility,
    count_code_migrations,
    run_doctor,
)
from hl_mem.errors import ConfigurationError
from hl_mem.settings import Settings, is_placeholder_secret
from hl_mem.storage.database import Database

HERMES_PLUGIN_FILES = ("__init__.py", "plugin.yaml", "contract.json")


def _copy_packaged_plugin(target: Path) -> None:
    source = Path(doctor_module.__file__).resolve().parent / "adapters" / "hermes" / "plugin"
    target.mkdir(parents=True)
    for name in HERMES_PLUGIN_FILES:
        (target / name).write_bytes((source / name).read_bytes())


def test_doctor_runs_without_crashing(tmp_path: Path, monkeypatch) -> None:
    """离线配置下 doctor 应完整返回所有检查结果。"""
    database_path = tmp_path / "doctor.db"
    database = Database(database_path)
    database.open()
    database.close()
    config_path = tmp_path / "hl_mem.toml"
    config_path.write_text('[recall]\nquery_expansion_mode = "off"\n', encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "hl_mem.doctor._check_port",
        lambda: CheckResult(CheckStatus.WARN, "服务端口", "测试跳过"),
    )
    monkeypatch.setattr(
        "hl_mem.doctor._probe_daemon",
        lambda settings: DaemonProbe(None, "测试离线"),
    )
    results = run_doctor(
        database_path=database_path,
        config_path=config_path,
        env_path=env_path,
        environ={},
    )
    assert len(results) == 12


def test_doctor_accepts_matching_daemon_and_wire_contracts() -> None:
    probe = DaemonProbe(
        {
            "version": "0.29.0",
            "compatibility": {
                "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
                "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
                "context_packet": {
                    "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
                    "schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
                },
            },
        },
        None,
    )

    assert _check_daemon_compatibility(probe).status is CheckStatus.OK
    assert _check_wire_compatibility(probe).status is CheckStatus.OK


def test_doctor_rejects_daemon_or_wire_major_mismatch() -> None:
    probe = DaemonProbe(
        {
            "version": "0.30.0",
            "compatibility": {
                "daemon_contract_major": DAEMON_CONTRACT_MAJOR + 1,
                "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR,
                "context_packet": {
                    "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR + 1,
                    "schema_minor": 0,
                },
            },
        },
        None,
    )

    assert _check_daemon_compatibility(probe).status is CheckStatus.FAIL
    assert _check_wire_compatibility(probe).status is CheckStatus.FAIL


def test_doctor_rejects_daemon_required_plugin_major_mismatch() -> None:
    probe = DaemonProbe(
        {
            "version": "0.29.0",
            "compatibility": {
                "daemon_contract_major": DAEMON_CONTRACT_MAJOR,
                "required_plugin_contract_major": HERMES_PLUGIN_CONTRACT_MAJOR + 1,
                "context_packet": {
                    "schema_major": CONTEXT_PACKET_SCHEMA_MAJOR,
                    "schema_minor": CONTEXT_PACKET_SCHEMA_MINOR,
                },
            },
        },
        None,
    )

    result = _check_daemon_compatibility(probe)

    assert result.status is CheckStatus.FAIL
    assert "required_plugin_contract_major" in result.detail


def test_doctor_warns_when_daemon_contract_cannot_be_observed() -> None:
    probe = DaemonProbe(None, "connection refused")

    assert _check_daemon_compatibility(probe).status is CheckStatus.WARN
    assert _check_wire_compatibility(probe).status is CheckStatus.WARN


def test_doctor_accepts_matching_installed_plugin_contract(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(plugin_dir)

    result = _check_plugin_compatibility(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.OK


def test_doctor_rejects_incompatible_installed_plugin_contract(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(plugin_dir)
    contract_path = plugin_dir / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["context_packet_schema_major"] = CONTEXT_PACKET_SCHEMA_MAJOR + 1
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    result = _check_plugin_compatibility(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert "context_packet_schema_major" in result.detail


def test_placeholder_secret_detection() -> None:
    """空值和常见模板值应被识别，真实格式密钥不应误报。"""
    for value in (None, "", "xxx", "your-key", "<API_KEY>", "sk-xxx", "sk-e72xxx"):
        assert is_placeholder_secret(value)
    assert not is_placeholder_secret("sk-live-abcdef123456")


def test_migration_count_matches_database(tmp_path: Path) -> None:
    """全新数据库应用的 migration 数应与代码文件数一致。"""
    database_path = tmp_path / "migrations.db"
    database = Database(database_path)
    database.open()
    database.close()
    with sqlite3.connect(database_path) as connection:
        applied = int(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0])
    assert applied == count_code_migrations()


def test_enabled_components_reject_placeholder_secrets() -> None:
    """启用真实组件时，任何环境都必须拒绝空值或占位密钥。"""
    settings = Settings(
        extractor_mode="llm",
        embedder_mode="real",
        reranker_mode="on",
        llm_api_key="sk-xxx",
        embedding_api_key="embedding-real-key",
        reranker_api_key="reranker-real-key",
    )
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        settings.validate()

    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        Settings(extractor_mode="real", llm_api_key="your-key").validate()


def test_hermes_check_uses_user_plugin_root_even_with_agent_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码 checkout 存在时仍应检查 HERMES_HOME 根下的用户插件。"""
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(expected)
    agent_checkout = tmp_path / "hermes-agent"
    agent_checkout.mkdir()
    (agent_checkout / "pyproject.toml").touch()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = _check_hermes(Settings())

    assert result == CheckResult(CheckStatus.OK, "Hermes 插件", f"路径正确且无漂移：{expected}")


def test_hermes_check_reports_legacy_plugin_path(tmp_path: Path) -> None:
    """插件位于 legacy 目录时应同时指出实际路径与期望路径。"""
    actual = tmp_path / "plugins" / "memory" / "hl_mem"
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(actual)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert str(actual) in result.detail
    assert str(expected) in result.detail


def test_hermes_check_reports_plugin_under_unmarked_agent_subdirectory(tmp_path: Path) -> None:
    """旧猜测逻辑装入无标志 hermes-agent 子目录时应报告实际与期望路径。"""
    actual = tmp_path / "hermes-agent" / "plugins" / "hl_mem"
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(actual)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert str(actual) in result.detail
    assert str(expected) in result.detail


def test_hermes_check_prefers_installed_legacy_copy_over_empty_expected_directory(tmp_path: Path) -> None:
    """空期望目录不能遮蔽 legacy 路径中的完整插件副本。"""
    expected = tmp_path / "plugins" / "hl_mem"
    expected.mkdir(parents=True)
    actual = tmp_path / "plugins" / "memory" / "hl_mem"
    _copy_packaged_plugin(actual)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result.status is CheckStatus.FAIL
    assert str(actual) in result.detail
    assert str(expected) in result.detail


def test_hermes_check_ignores_empty_legacy_directory(tmp_path: Path) -> None:
    """空 legacy 目录不是已安装插件，不能被报告为实际路径。"""
    expected = tmp_path / "plugins" / "hl_mem"
    (tmp_path / "plugins" / "memory" / "hl_mem").mkdir(parents=True)

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result == CheckResult(CheckStatus.FAIL, "Hermes 插件", f"应安装到 {expected}")


def test_hermes_check_reports_plugin_drift(tmp_path: Path) -> None:
    """期望目录中的副本与包内文件不同应提示 upgrade。"""
    expected = tmp_path / "plugins" / "hl_mem"
    _copy_packaged_plugin(expected)
    (expected / "plugin.yaml").write_bytes(b"drifted")

    result = _check_hermes(Settings(hermes_home=str(tmp_path)))

    assert result == CheckResult(
        CheckStatus.FAIL,
        "Hermes 插件",
        "检测到插件副本漂移，运行 hl-mem hermes upgrade",
    )
