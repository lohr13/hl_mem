"""Hermes 插件副本的部署与一致性检查。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Literal, Sequence

from hl_mem import __version__
from hl_mem.adapters.hermes.discovery import find_hermes_home
from hl_mem.adapters.hermes.runtime_status import runtime_status_path

PLUGIN_FILES = ("__init__.py", "plugin.yaml", "contract.json")
PLUGIN_SOURCE_DIR = Path(__file__).resolve().parent / "plugin"
DeploymentAction = Literal["install", "upgrade"]


@dataclass(frozen=True)
class DeploymentResult:
    """一次 Hermes 插件部署的结构化结果。"""

    action: DeploymentAction
    hermes_home: Path
    target_dir: Path
    changed: bool
    dry_run: bool
    backup_dir: Path | None = None


def plugin_files_match(target_dir: Path) -> bool:
    """返回目标目录中的插件文件是否与包内副本逐字节一致。"""
    return all(
        (PLUGIN_SOURCE_DIR / name).is_file()
        and (target_dir / name).is_file()
        and (PLUGIN_SOURCE_DIR / name).read_bytes() == (target_dir / name).read_bytes()
        for name in PLUGIN_FILES
    )


def plugin_files_present(target_dir: Path) -> bool:
    """返回目标目录是否包含一份完整的插件文件集合。"""
    return all((target_dir / name).is_file() for name in PLUGIN_FILES)


def _editable_pth_files() -> list[Path]:
    return [pth_file for entry in sys.path for pth_file in Path(entry or ".").glob("*_editable_impl_hl_mem.pth")]


def _direct_url_is_editable(distribution: metadata.Distribution) -> bool:
    try:
        direct_url = distribution.read_text("direct_url.json")
        payload = json.loads(direct_url) if direct_url else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    dir_info = payload.get("dir_info") if isinstance(payload, dict) else None
    return isinstance(dir_info, dict) and dir_info.get("editable") is True


def _editable_source_tree(pth_files: Sequence[Path]) -> Path | None:
    for pth_file in pth_files:
        try:
            lines = pth_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            value = line.strip()
            if not value or value.startswith("import "):
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = pth_file.parent / candidate
            try:
                if candidate.is_dir():
                    return candidate.resolve()
            except OSError:
                continue
    return None


def _parse_systemd_timestamp(value: str) -> datetime | None:
    fields = value.split()
    try:
        parsed = datetime.fromisoformat(value if len(fields) == 1 else f"{fields[1]}T{fields[2]}")
    except (IndexError, ValueError):
        return None
    if len(fields) > 3 and fields[3].upper() in {"UTC", "GMT"}:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _gateway_active_since() -> datetime | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        completed = subprocess.run(
            ["systemctl", "show", "hermes-gateway", "-p", "ActiveEnterTimestamp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    value = output.partition("=")[2].strip() if "=" in output else output
    return _parse_systemd_timestamp(value)


def audit_deployment_health(hermes_home: Path) -> list[str]:
    """只读检查当前解释器与 Hermes 插件部署是否一致、新鲜。"""
    warnings: list[str] = []
    pth_files = _editable_pth_files()
    try:
        distribution = metadata.distribution("hl_mem")
    except (metadata.PackageNotFoundError, OSError):
        distribution = None
    editable = bool(pth_files) or (distribution is not None and _direct_url_is_editable(distribution))
    if editable:
        try:
            dist_info_version = metadata.version("hl_mem")
        except (metadata.PackageNotFoundError, OSError):
            dist_info_version = None
        if dist_info_version is not None and dist_info_version != __version__:
            warnings.append(
                f"venv dist-info 版本 {dist_info_version} 残留，实际运行 {__version__}；"
                "修复=在目标 venv 重跑 pip install -e . 刷新元数据"
            )
        source_tree = _editable_source_tree(pth_files)
        gateway_started = _gateway_active_since() if source_tree is not None else None
        try:
            tree_mtime = source_tree.stat().st_mtime if source_tree is not None else None
        except OSError:
            tree_mtime = None
        if tree_mtime is not None and gateway_started is not None and tree_mtime > gateway_started.timestamp():
            warnings.append("editable 树在网关启动后被修改，须重启网关")
    target_dir = Path(hermes_home).expanduser().resolve() / "plugins" / "hl_mem"
    try:
        copies_match = plugin_files_match(target_dir)
    except OSError:
        copies_match = False
    if not copies_match:
        warnings.append("Hermes 插件副本与包内模板不一致；请运行 hl-mem hermes upgrade")
    return warnings


def _print_deployment_health(hermes_home: Path) -> None:
    try:
        warnings = audit_deployment_health(hermes_home)
    except Exception as error:
        print(f"WARNING: Hermes 部署体检不可用（{type(error).__name__}）")
        return
    for warning in warnings:
        print(f"WARNING: {warning}")


def backup_existing(target_dir: Path) -> Path | None:
    """备份目标目录内已有的插件文件，并返回备份目录。"""
    existing = [target_dir / filename for filename in PLUGIN_FILES if (target_dir / filename).is_file()]
    if not existing:
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = target_dir / f"backup_{timestamp}"
    suffix = 1
    while backup_dir.exists():
        backup_dir = target_dir / f"backup_{timestamp}_{suffix}"
        suffix += 1
    backup_dir.mkdir(parents=True)
    for source in existing:
        shutil.copy2(source, backup_dir / source.name)
    return backup_dir


def _validate_source_files() -> None:
    missing = [str(PLUGIN_SOURCE_DIR / name) for name in PLUGIN_FILES if not (PLUGIN_SOURCE_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing source files: {', '.join(missing)}")


def _copy_plugin(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        source = PLUGIN_SOURCE_DIR / name
        destination = target_dir / name
        shutil.copy2(source, destination)
        if source.read_bytes() != destination.read_bytes():
            raise RuntimeError(f"Verification failed after copying {name} to {destination}")


def _print_restart_requirement() -> None:
    print("Required: restart every running Hermes gateway/CLI process that imported hl_mem.")
    print("Do not validate a new hl_mem.toml against a process that imported an older editable checkout.")


def _print_configuration_ownership(result: DeploymentResult) -> None:
    config_path = (result.hermes_home / "hl_mem.toml").resolve()
    env_path = (result.hermes_home / ".env").resolve()
    config_state = "present" if config_path.is_file() else "missing"
    env_state = "present" if env_path.is_file() else "missing"
    print(f"Hermes config ({config_state}): {config_path}")
    print(f"Hermes secrets ({env_state}): {env_path}")
    print(f'Validate: hl-mem doctor --config "{config_path}" --env-file "{env_path}"')
    print("Repository .env is not used by the Hermes plugin.")
    print(f"Hermes runtime evidence: {runtime_status_path(result.hermes_home)}")


def deploy_plugin(
    action: DeploymentAction,
    hermes_home: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> DeploymentResult:
    """安装或升级 Hermes 插件；一致副本始终保持 no-op。"""
    _validate_source_files()
    resolved_home = find_hermes_home(hermes_home)
    target_dir = (resolved_home / "plugins" / "hl_mem").resolve()
    if plugin_files_match(target_dir):
        return DeploymentResult(action, resolved_home, target_dir, changed=False, dry_run=dry_run)
    if action == "install" and target_dir.exists():
        raise RuntimeError(f"Hermes plugin at {target_dir} differs from the packaged copy; run hl-mem hermes upgrade")
    if dry_run:
        return DeploymentResult(action, resolved_home, target_dir, changed=True, dry_run=True)

    backup_dir = backup_existing(target_dir) if action == "upgrade" else None
    _copy_plugin(target_dir)
    return DeploymentResult(action, resolved_home, target_dir, changed=True, dry_run=False, backup_dir=backup_dir)


def print_deployment_result(result: DeploymentResult) -> None:
    """以适合 CLI 的文本输出部署结果。"""
    if not result.changed:
        print("Hermes plugin already current; no changes")
    elif result.dry_run:
        print(f"Dry run: would {result.action} {', '.join(PLUGIN_FILES)}")
    else:
        print(f"Hermes plugin {result.action} succeeded")
    print(f"Target (absolute): {result.target_dir}")
    if result.action == "upgrade" and result.changed:
        if result.dry_run and any((result.target_dir / name).is_file() for name in PLUGIN_FILES):
            backup: str | Path = "existing files would be backed up"
        else:
            backup = result.backup_dir if result.backup_dir is not None else "not required"
        print(f"Backup: {backup}")
    _print_configuration_ownership(result)
    if not result.dry_run:
        _print_restart_requirement()
    _print_deployment_health(result.hermes_home)


def script_main(argv: Sequence[str] | None = None) -> int:
    """兼容原独立安装脚本的命令行入口。"""
    parser = argparse.ArgumentParser(description="Install or upgrade the HL-Mem Hermes plugin")
    parser.add_argument("--hermes-home", type=Path, help="Hermes agent root directory")
    parser.add_argument("--dry-run", action="store_true", help="Preview installation without writing files")
    args = parser.parse_args(argv)
    try:
        hermes_home = find_hermes_home(args.hermes_home)
        target_dir = (hermes_home / "plugins" / "hl_mem").resolve()
        legacy_dir = (hermes_home / "plugins" / "memory" / "hl_mem").resolve()
        if legacy_dir.exists():
            print(
                f"Migration notice: legacy plugin found at {legacy_dir}; "
                f"after installation, remove or archive it and use {target_dir}"
            )
        if args.dry_run:
            print_deployment_result(deploy_plugin("upgrade", hermes_home, dry_run=True))
            return 0

        print(f"Installing HL-Mem Hermes plugin to {target_dir}")
        result = deploy_plugin("upgrade", hermes_home)
        print("Installation succeeded")
        print(f"Installed: {', '.join(PLUGIN_FILES)}")
        print(f"Target (absolute): {target_dir}")
        print(f"Backup: {result.backup_dir if result.backup_dir else 'not required'}")
        print("Verification: source and installed files match")
        _print_configuration_ownership(result)
        _print_restart_requirement()
        _print_deployment_health(hermes_home)
        return 0
    except Exception as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
