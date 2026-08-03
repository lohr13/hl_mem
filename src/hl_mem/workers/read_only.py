"""维护命令的只读 SQLite 连接工具。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_read_only_connection(
    database_path: str | Path,
    *,
    busy_timeout_seconds: float,
) -> sqlite3.Connection:
    """打开既有数据库且禁止写入，不创建文件、不执行 migration。"""
    resolved = Path(database_path).expanduser().resolve()
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_seconds * 1000))}")
    connection.execute("PRAGMA query_only=ON")
    return connection
