"""Hermes 根目录探测，仅依赖 Python 标准库。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_hermes_home(arg_override: str | Path | None) -> Path:
    """按参数、环境变量和平台默认值定位 Hermes 用户数据根目录。"""
    if arg_override:
        return Path(arg_override).expanduser().resolve()
    environment_home = os.getenv("HERMES_HOME", "").strip()
    if environment_home:
        return Path(environment_home).expanduser().resolve()
    if sys.platform == "win32":
        local_appdata = os.getenv("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return (base / "hermes").resolve()
    return (Path.home() / ".hermes").resolve()
