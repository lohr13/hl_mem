"""Hermes 根目录探测，仅依赖 Python 标准库。"""

from __future__ import annotations

import os
from pathlib import Path


def is_hermes_home(path: Path) -> bool:
    """检查路径是否具有 Hermes 根目录的标志。"""
    return (path / "plugins").is_dir() or any(
        (path / marker).exists() for marker in ("hermes.py", "cli.py", "pyproject.toml", "config.yaml")
    )


def find_hermes_home(arg_override: str | Path | None) -> Path:
    """按参数、环境变量和常见目录的优先级定位 Hermes 根目录。"""
    if arg_override:
        return Path(arg_override).expanduser().resolve()
    environment_home = os.getenv("HERMES_HOME")
    if environment_home:
        base = Path(environment_home).expanduser().resolve()
        agent_dir = base / "hermes-agent"
        if is_hermes_home(agent_dir):
            return agent_dir
        if is_hermes_home(base):
            return base
        return base
    candidates = [
        Path("C:/Users/Administrator/AppData/Local/hermes"),
        Path.home() / ".hermes",
        Path.home() / "AppData" / "Local" / "hermes",
    ]
    for candidate in candidates:
        agent_dir = candidate / "hermes-agent"
        if is_hermes_home(agent_dir):
            return agent_dir.resolve()
        if is_hermes_home(candidate):
            return candidate.resolve()
    tried = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Cannot find HERMES_HOME. Tried: {tried}")
