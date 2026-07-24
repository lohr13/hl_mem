"""实验性 PostgreSQL 连接探针；不提供 HL-Mem 仓储语义。"""

from __future__ import annotations

from typing import Any

EXPERIMENTAL = True


class PostgresDatabase:
    """实验性的 PostgreSQL 连接探针，尚未实现 HL-Mem 仓储语义。"""

    def __init__(self, dsn: str, connect_timeout: float = 5.0) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.connection: Any = None

    def open(self) -> Any:
        """打开 PostgreSQL 连接；未安装可选驱动时给出明确提示。"""
        if self.connection is not None:
            return self.connection
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("PostgreSQL backend requires the optional psycopg package") from error
        self.connection = psycopg.connect(self.dsn, connect_timeout=self.connect_timeout)
        return self.connection

    def close(self) -> None:
        """关闭当前 PostgreSQL 连接。"""
        if self.connection is not None:
            self.connection.close()
            self.connection = None
