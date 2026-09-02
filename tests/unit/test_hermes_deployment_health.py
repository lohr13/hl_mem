"""Hermes 部署尾部只读体检的单元测试。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hl_mem import __version__
from hl_mem.adapters.hermes import deployment
from hl_mem.adapters.hermes.deployment import PLUGIN_FILES, PLUGIN_SOURCE_DIR, audit_deployment_health
from hl_mem.cli import main as cli_main
from scripts import install_to_hermes


class _Distribution:
    def __init__(self, *, editable: bool) -> None:
        self.editable = editable

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json" or not self.editable:
            return None
        return '{"url":"file:///checkout","dir_info":{"editable":true}}'


def _install_matching_plugin(hermes_home: Path) -> None:
    target = hermes_home / "plugins" / "hl_mem"
    target.mkdir(parents=True)
    for name in PLUGIN_FILES:
        (target / name).write_bytes((PLUGIN_SOURCE_DIR / name).read_bytes())


def _patch_metadata(
    monkeypatch: pytest.MonkeyPatch,
    *,
    editable: bool,
    dist_info_version: str,
) -> None:
    monkeypatch.setattr(deployment.metadata, "distribution", lambda _name: _Distribution(editable=editable))
    monkeypatch.setattr(deployment.metadata, "version", lambda _name: dist_info_version)
    monkeypatch.setattr(deployment, "_editable_pth_files", lambda: [])


def test_audit_warns_when_editable_dist_info_version_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_matching_plugin(tmp_path)
    _patch_metadata(monkeypatch, editable=True, dist_info_version="0.29.3")
    monkeypatch.setattr(deployment.sys, "platform", "win32")

    warnings = audit_deployment_health(tmp_path)

    assert warnings == [
        f"venv dist-info 版本 0.29.3 残留，实际运行 {__version__}；" "修复=在目标 venv 重跑 pip install -e . 刷新元数据"
    ]


def test_audit_returns_empty_when_deployment_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_matching_plugin(tmp_path)
    _patch_metadata(monkeypatch, editable=True, dist_info_version=__version__)
    monkeypatch.setattr(deployment.sys, "platform", "win32")

    assert audit_deployment_health(tmp_path) == []


def test_audit_skips_tree_freshness_when_systemctl_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_matching_plugin(tmp_path)
    source_tree = tmp_path / "checkout" / "src"
    source_tree.mkdir(parents=True)
    pth_file = tmp_path / "_editable_impl_hl_mem.pth"
    pth_file.write_text(str(source_tree), encoding="utf-8")
    _patch_metadata(monkeypatch, editable=True, dist_info_version=__version__)
    monkeypatch.setattr(deployment, "_editable_pth_files", lambda: [pth_file])
    monkeypatch.setattr(deployment.sys, "platform", "linux")

    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("systemctl not installed")

    monkeypatch.setattr(deployment.subprocess, "run", unavailable)

    assert audit_deployment_health(tmp_path) == []


def test_audit_warns_when_editable_tree_changed_after_gateway_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_matching_plugin(tmp_path)
    source_tree = tmp_path / "checkout" / "src"
    source_tree.mkdir(parents=True)
    os.utime(source_tree, (1_788_345_000, 1_788_345_000))
    pth_file = tmp_path / "_editable_impl_hl_mem.pth"
    pth_file.write_text(str(source_tree), encoding="utf-8")
    _patch_metadata(monkeypatch, editable=True, dist_info_version=__version__)
    monkeypatch.setattr(deployment, "_editable_pth_files", lambda: [pth_file])
    monkeypatch.setattr(deployment.sys, "platform", "linux")
    completed = SimpleNamespace(
        returncode=0,
        stdout="ActiveEnterTimestamp=Wed 2026-09-02 08:00:00 UTC\n",
    )

    def systemctl_show(*args: object, **kwargs: object) -> SimpleNamespace:
        assert kwargs["timeout"] == 5
        return completed

    monkeypatch.setattr(deployment.subprocess, "run", systemctl_show)

    warnings = audit_deployment_health(tmp_path)

    assert warnings == ["editable 树在网关启动后被修改，须重启网关"]


def test_audit_warns_when_installed_plugin_copy_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_metadata(monkeypatch, editable=False, dist_info_version=__version__)
    monkeypatch.setattr(deployment.sys, "platform", "win32")

    assert audit_deployment_health(tmp_path) == ["Hermes 插件副本与包内模板不一致；请运行 hl-mem hermes upgrade"]


def test_install_script_prints_health_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deployment, "audit_deployment_health", lambda _home: ["deployment warning"])

    exit_code = install_to_hermes.main(["--hermes-home", str(tmp_path), "--dry-run"])

    assert exit_code == 0
    assert "WARNING: deployment warning" in capsys.readouterr().out


def test_cli_hermes_upgrade_prints_health_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deployment, "audit_deployment_health", lambda _home: ["deployment warning"])

    cli_main(["hermes", "upgrade", "--hermes-home", str(tmp_path), "--dry-run"])

    assert "WARNING: deployment warning" in capsys.readouterr().out


@pytest.mark.parametrize("entrypoint", ["script", "cli"])
def test_health_audit_failure_does_not_reclassify_successful_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    def unavailable(_home: Path) -> list[str]:
        raise ValueError("malformed deployment metadata")

    monkeypatch.setattr(deployment, "audit_deployment_health", unavailable)

    if entrypoint == "script":
        assert install_to_hermes.main(["--hermes-home", str(tmp_path), "--dry-run"]) == 0
    else:
        cli_main(["hermes", "upgrade", "--hermes-home", str(tmp_path), "--dry-run"])

    output = capsys.readouterr().out
    assert "WARNING: Hermes 部署体检不可用（ValueError）" in output
