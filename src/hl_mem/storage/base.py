"""存储后端的最小连接契约。"""

from __future__ import annotations

from typing import Any, Protocol


class StorageDatabase(Protocol):
    """SQLite 与可选生产后端共同遵循的连接协议。"""

    def open(self) -> Any:
        """打开并返回数据库连接。"""

    def close(self) -> None:
        """关闭数据库连接。"""
