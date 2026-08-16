"""Hermes 插件副本的部署与一致性检查。"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from hl_mem.adapters.hermes.discovery import find_hermes_home

PLUGIN_FILES = ("__init__.py", "plugin.yaml")
PLUGIN_SOURCE_DIR = Path(__file__).resolve().parent / "plugin"
DeploymentAction = Literal["install", "upgrade"]


@dataclass(frozen=True)
class DeploymentResult:
    """一次 Hermes 插件部署的结构化结果。"""

    action: DeploymentAction
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
        return DeploymentResult(action, target_dir, changed=False, dry_run=dry_run)
    if action == "install" and target_dir.exists():
        raise RuntimeError(f"Hermes plugin at {target_dir} differs from the packaged copy; run hl-mem hermes upgrade")
    if dry_run:
        return DeploymentResult(action, target_dir, changed=True, dry_run=True)

    backup_dir = backup_existing(target_dir) if action == "upgrade" else None
    _copy_plugin(target_dir)
    return DeploymentResult(action, target_dir, changed=True, dry_run=False, backup_dir=backup_dir)


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
        return 0
    except Exception as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
