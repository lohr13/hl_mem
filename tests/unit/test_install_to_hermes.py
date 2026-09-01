"""Hermes 插件安装脚本的单元测试。"""

from pathlib import Path

import pytest

from hl_mem.adapters.hermes.deployment import PLUGIN_FILES, PLUGIN_SOURCE_DIR
from hl_mem.cli import main as cli_main
from scripts import install_to_hermes


def _assert_installed_plugin_matches_package(target: Path) -> None:
    for name in PLUGIN_FILES:
        assert (target / name).read_bytes() == (PLUGIN_SOURCE_DIR / name).read_bytes()


def test_main_prints_start_and_success_messages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """实际安装应在开始时提示，并在校验成功后打印完成提示。"""
    exit_code = install_to_hermes.main(["--hermes-home", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Installing HL-Mem Hermes plugin" in output
    assert "Installation succeeded" in output
    assert "Required: restart every running Hermes gateway/CLI process that imported hl_mem." in output
    assert "Do not validate a new hl_mem.toml against a process that imported an older editable checkout." in output
    assert output.index("Installing HL-Mem Hermes plugin") < output.index("Installation succeeded")


@pytest.mark.parametrize("action", ["install", "upgrade"])
def test_cli_hermes_changed_deployment_requires_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    """真实写入插件后必须阻止用户在旧宿主进程中验证新配置。"""
    cli_main(["hermes", action, "--hermes-home", str(tmp_path)])

    output = capsys.readouterr().out
    assert "Required: restart every running Hermes gateway/CLI process that imported hl_mem." in output
    assert "Do not validate a new hl_mem.toml against a process that imported an older editable checkout." in output


def test_cli_hermes_install_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """二次 install 应检测一致副本并保持 no-op。"""
    target = tmp_path / "plugins" / "hl_mem"

    cli_main(["hermes", "install", "--hermes-home", str(tmp_path)])
    _assert_installed_plugin_matches_package(target)
    assert list(target.glob("backup_*")) == []
    capsys.readouterr()

    cli_main(["hermes", "install", "--hermes-home", str(tmp_path)])

    output = capsys.readouterr().out
    assert "no changes" in output
    assert "Required: restart every running Hermes gateway/CLI process that imported hl_mem." in output
    assert list(target.glob("backup_*")) == []
    _assert_installed_plugin_matches_package(target)


@pytest.mark.parametrize("action", ["install", "upgrade"])
def test_cli_hermes_deploys_to_user_plugin_root_even_with_agent_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    """环境探测不得把 install/upgrade 目标改写到源码 checkout 下。"""
    agent_checkout = tmp_path / "hermes-agent"
    agent_checkout.mkdir()
    (agent_checkout / "pyproject.toml").touch()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    cli_main(["hermes", action])

    capsys.readouterr()
    _assert_installed_plugin_matches_package(tmp_path / "plugins" / "hl_mem")
    assert not (agent_checkout / "plugins" / "hl_mem").exists()


def test_cli_hermes_install_dry_run_does_not_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """install --dry-run 应报告计划但不创建插件目录。"""
    target = tmp_path / "plugins" / "hl_mem"

    cli_main(["hermes", "install", "--hermes-home", str(tmp_path), "--dry-run"])

    assert "Dry run" in capsys.readouterr().out
    assert not target.exists()


@pytest.mark.parametrize(
    ("action", "dry_run"),
    [("install", False), ("upgrade", False), ("install", True)],
)
@pytest.mark.parametrize("configuration_present", [False, True])
def test_cli_hermes_reports_owned_configuration_paths_without_secret_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
    dry_run: bool,
    configuration_present: bool,
) -> None:
    config_path = (tmp_path / "hl_mem.toml").resolve()
    env_path = (tmp_path / ".env").resolve()
    if configuration_present:
        config_path.write_text("schema_version = 1\n", encoding="utf-8")
        env_path.write_text("LLM_API_KEY=must-not-be-printed\n", encoding="utf-8")

    arguments = ["hermes", action, "--hermes-home", str(tmp_path)]
    if dry_run:
        arguments.append("--dry-run")
    cli_main(arguments)

    output = capsys.readouterr().out
    state = "present" if configuration_present else "missing"
    assert f"Hermes config ({state}): {config_path}" in output
    assert f"Hermes secrets ({state}): {env_path}" in output
    assert f'hl-mem doctor --config "{config_path}" --env-file "{env_path}"' in output
    assert "Repository .env is not used by the Hermes plugin." in output
    assert "must-not-be-printed" not in output


def test_cli_hermes_install_refuses_to_overwrite_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """install 不得静默覆盖已有漂移副本，升级必须显式执行。"""
    target = tmp_path / "plugins" / "hl_mem"
    target.mkdir(parents=True)
    drifted = b"operator-edited"
    (target / "__init__.py").write_bytes(drifted)

    with pytest.raises(SystemExit) as raised:
        cli_main(["hermes", "install", "--hermes-home", str(tmp_path)])

    assert raised.value.code == 1
    assert "hl-mem hermes upgrade" in capsys.readouterr().err
    assert (target / "__init__.py").read_bytes() == drifted
    assert not (target / "plugin.yaml").exists()


def test_cli_hermes_upgrade_dry_run_preserves_drift_and_reports_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """upgrade --dry-run 应只报告覆盖与备份计划，不写入磁盘。"""
    target = tmp_path / "plugins" / "hl_mem"
    target.mkdir(parents=True)
    drifted = b"operator-edited"
    (target / "__init__.py").write_bytes(drifted)

    cli_main(["hermes", "upgrade", "--hermes-home", str(tmp_path), "--dry-run"])

    output = capsys.readouterr().out
    assert "Dry run" in output
    assert "existing files would be backed up" in output
    assert (target / "__init__.py").read_bytes() == drifted
    assert not (target / "plugin.yaml").exists()
    assert list(target.glob("backup_*")) == []


def test_cli_hermes_upgrade_backs_up_drift_then_becomes_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """upgrade 应备份漂移文件、覆盖为包内字节，随后保持幂等。"""
    target = tmp_path / "plugins" / "hl_mem"
    target.mkdir(parents=True)
    old_bytes = {name: f"old-{name}".encode() for name in PLUGIN_FILES}
    for name, content in old_bytes.items():
        (target / name).write_bytes(content)

    cli_main(["hermes", "upgrade", "--hermes-home", str(tmp_path)])

    [backup] = list(target.glob("backup_*"))
    for name, content in old_bytes.items():
        assert (backup / name).read_bytes() == content
    _assert_installed_plugin_matches_package(target)
    capsys.readouterr()

    cli_main(["hermes", "upgrade", "--hermes-home", str(tmp_path)])

    assert "no changes" in capsys.readouterr().out
    assert list(target.glob("backup_*")) == [backup]
    _assert_installed_plugin_matches_package(target)
