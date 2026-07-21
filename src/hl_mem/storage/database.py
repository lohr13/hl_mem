from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_database_path() -> Path:
    """返回环境变量配置或项目 var 目录下的默认数据库路径。"""
    configured = os.getenv("HL_MEM_DB_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "var" / "hl_mem.db"


class Database:
    """Own a SQLite connection and apply ordered SQL migrations."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(Path(path) if path is not None else default_database_path())
        self.connection: sqlite3.Connection | None = None

    def open(self) -> sqlite3.Connection:
        if self.connection is not None:
            return self.connection
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0:
            has_tables = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone()
            if not has_tables:
                connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        self.connection = connection
        self._migrate()
        return connection

    def _migrate(self) -> None:
        assert self.connection is not None
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        migration_dir = Path(__file__).with_name("migrations")
        for migration in sorted(migration_dir.glob("*.sql")):
            version = migration.stem
            found = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if found:
                continue
            self.connection.executescript(migration.read_text(encoding="utf-8"))
            self.connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
            self.connection.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self) -> sqlite3.Connection:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()
