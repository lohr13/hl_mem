#!/usr/bin/env python3
"""将 HL-Mem 适配器安装或升级到 Hermes 插件目录。

Usage:
    python install_to_hermes.py [--hermes-home PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.resolve()
SOURCE_DIR = REPO_ROOT / "src" / "hl_mem" / "adapters" / "hermes" / "plugin"
FILES = ("__init__.py", "plugin.yaml")


def find_hermes_home(arg_override: str | Path | None) -> Path:
    """按参数、环境变量和常见目录的优先级定位 Hermes 根目录。"""
    if arg_override:
        return Path(arg_override).expanduser().resolve()
    environment_home = os.getenv("HERMES_HOME")
    if environment_home:
        base = Path(environment_home).expanduser().resolve()
        agent_dir = base / "hermes-agent"
        if (agent_dir / "plugins" / "memory").exists():
            return agent_dir
        if (base / "plugins" / "memory").exists():
            return base
        return base
    candidates = [
        Path("C:/Users/Administrator/AppData/Local/hermes"),
        Path.home() / ".hermes",
        Path.home() / "AppData" / "Local" / "hermes",
    ]
    for candidate in candidates:
        agent_dir = candidate / "hermes-agent"
        if (agent_dir / "plugins" / "memory").exists():
            return agent_dir.resolve()
        if (candidate / "plugins" / "memory").exists():
            return candidate.resolve()
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Cannot find HERMES_HOME. Tried: {tried}")


def backup_existing(target_dir: Path) -> Path | None:
    """备份目标目录内已有的插件文件，并返回备份目录。"""
    existing = [target_dir / filename for filename in FILES if (target_dir / filename).is_file()]
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


def install(target_dir: Path, dry_run: bool = False) -> Path | None:
    """备份并安装插件文件，复制后逐字节验证内容。"""
    missing = [str(SOURCE_DIR / filename) for filename in FILES if not (SOURCE_DIR / filename).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing source files: {', '.join(missing)}")
    if dry_run:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = backup_existing(target_dir)
    for filename in FILES:
        source = SOURCE_DIR / filename
        destination = target_dir / filename
        shutil.copy2(source, destination)
        if source.read_bytes() != destination.read_bytes():
            raise RuntimeError(f"Verification failed after copying {filename} to {destination}")
    return backup_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析安装脚本命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, help="Hermes agent root directory")
    parser.add_argument("--dry-run", action="store_true", help="Preview installation without writing files")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行安装并返回适合命令行使用的退出码。"""
    try:
        args = parse_args(argv)
        hermes_home = find_hermes_home(args.hermes_home)
        target_dir = hermes_home / "plugins" / "memory" / "hl_mem"
        if args.dry_run:
            missing = [SOURCE_DIR / filename for filename in FILES if not (SOURCE_DIR / filename).is_file()]
            if missing:
                raise FileNotFoundError(f"Missing source files: {', '.join(map(str, missing))}")
            print(f"Dry run: would install {', '.join(FILES)}")
            print(f"Source: {SOURCE_DIR}")
            print(f"Target: {target_dir}")
            existing = [filename for filename in FILES if (target_dir / filename).is_file()]
            print(f"Backup: {'existing files would be backed up' if existing else 'not required'}")
            return 0

        backup_dir = install(target_dir)
        print(f"Installed: {', '.join(FILES)}")
        print(f"Target: {target_dir}")
        print(f"Backup: {backup_dir if backup_dir else 'not required'}")
        print("Verification: source and installed files match")
        return 0
    except Exception as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
