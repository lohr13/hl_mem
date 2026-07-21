from __future__ import annotations

import sqlite3
from pathlib import Path


class Database:
    """Own a SQLite connection and apply ordered SQL migrations."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
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
