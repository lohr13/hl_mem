"""SQLite WAL 数据库、迁移执行与连接生命周期管理。"""

from __future__ import annotations

import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hl_mem.settings import Settings, VectorBackend
from hl_mem.storage.migrations.backfill_conflict_key_v2 import backfill_conflict_keys_v2
from hl_mem.storage.migrations.backfill_conflict_key_v3 import backfill_conflict_keys_v3
from hl_mem.storage.migrations.fact_hash_v2 import backfill_fact_hash_v2
from hl_mem.storage.migrations.sqlite_vec import (
    disable_sqlite_vec,
    ensure_vector_control_schema,
    migrate_sqlite_vec,
)
from hl_mem.storage.sqlite_vec import load_sqlite_vec_extension
from hl_mem.storage.tokenized_fts import ensure_tokenized_fts_v2


def default_database_path(settings: Settings | None = None) -> Path:
    """返回 Settings 中声明的数据库路径。"""
    return Path((settings or Settings()).database_path)


class HLMemoryConnection(sqlite3.Connection):
    """携带创建时 Settings 的 SQLite connection。"""

    hl_mem_settings: Settings


class Database:
    """管理 SQLite 迁移、专用连接和请求级连接池。"""

    def __init__(
        self,
        path: str | Path | None = None,
        pool_size: int | None = None,
        busy_timeout_seconds: float | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        resolved_settings = settings or Settings()
        self.settings = resolved_settings
        self.path = str(Path(path) if path is not None else default_database_path(resolved_settings))
        self.pool_size = pool_size if pool_size is not None else resolved_settings.database_pool_size
        self.busy_timeout_seconds = (
            busy_timeout_seconds
            if busy_timeout_seconds is not None
            else float(resolved_settings.database_busy_timeout_seconds)
        )
        self.busy_timeout_ms = int(self.busy_timeout_seconds * 1000)
        self.connection: sqlite3.Connection | None = None
        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=self.pool_size)
        self._connections: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()
        self._migrated = False

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            check_same_thread=False,
            factory=HLMemoryConnection,
        )
        connection.hl_mem_settings = self.settings
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        try:
            if VectorBackend(self.settings.vector_backend) is VectorBackend.SQLITE_VEC:
                load_sqlite_vec_extension(connection)
        except Exception:
            connection.close()
            raise
        with self._lock:
            self._connections.add(connection)
        return connection

    def _ensure_migrated(self) -> None:
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._migrated:
                return
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_seconds,
                factory=HLMemoryConnection,
            )
            connection.hl_mem_settings = self.settings
            connection.row_factory = sqlite3.Row
            try:
                if connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0:
                    has_tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
                    if not has_tables:
                        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                if VectorBackend(self.settings.vector_backend) is VectorBackend.SQLITE_VEC:
                    load_sqlite_vec_extension(connection)
                self._migrate(connection)
                self._migrated = True
            finally:
                connection.close()

    def open(self) -> sqlite3.Connection:
        """返回一个独立连接；调用方负责关闭或交回连接池。"""
        self._ensure_migrated()
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            return self._new_connection()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """获取请求级连接，并在退出时回滚残留事务后归还连接池。"""
        connection = self.open()
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            try:
                self._pool.put_nowait(connection)
            except queue.Full:
                connection.close()
                with self._lock:
                    self._connections.discard(connection)

    def open_worker(self) -> sqlite3.Connection:
        """返回供 worker 生命周期独占的全局连接。"""
        self._ensure_migrated()
        if self.connection is None:
            self.connection = self._new_connection()
        return self.connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.commit()
        migration_dir = Path(__file__).with_name("migrations")
        for migration in sorted(migration_dir.glob("*.sql")):
            version = migration.stem
            try:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                    connection.commit()
                    continue
                statement = ""
                for line in migration.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise sqlite3.OperationalError(f"incomplete SQL in migration {version}")
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version='006_canonical_attribute'").fetchone():
            backfill_conflict_keys_v2(connection)
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version='011_fact_hash_v2'").fetchone():
            backfill_fact_hash_v2(connection)
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version='016_claim_slots_and_tags'").fetchone():
            backfill_conflict_keys_v3(connection)
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version='036_tokenized_fts_v2'").fetchone():
            ensure_tokenized_fts_v2(connection)
        ensure_vector_control_schema(connection)
        if VectorBackend(self.settings.vector_backend) is VectorBackend.SQLITE_VEC:
            extension_version = load_sqlite_vec_extension(connection)
            migrate_sqlite_vec(
                connection,
                self.settings.embedding_dim,
                self.settings.embedding_model,
                extension_version,
            )
        else:
            disable_sqlite_vec(connection)

    def close(self) -> None:
        """关闭本实例创建的全部连接。"""
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
            self.connection = None
        while True:
            try:
                self._pool.get_nowait()
            except queue.Empty:
                break
        for connection in connections:
            connection.close()
