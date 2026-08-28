"""SQLite WAL 数据库、迁移执行与连接生命周期管理。"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hl_mem.domain.entity_coordinates import normalize_typed_alias
from hl_mem.observability.audit import current_audit_dimension
from hl_mem.settings import Settings, VectorBackend
from hl_mem.storage.migrations.backfill_conflict_key_v2 import backfill_conflict_keys_v2
from hl_mem.storage.migrations.backfill_conflict_key_v3 import backfill_conflict_keys_v3
from hl_mem.storage.migrations.backfill_subject_canonicalization import (
    backfill_subject_canonicalization,
)
from hl_mem.storage.migrations.fact_hash_v2 import backfill_fact_hash_v2
from hl_mem.storage.migrations.sqlite_vec import (
    disable_sqlite_vec,
    migrate_sqlite_vec,
)
from hl_mem.storage.sqlite_vec import drain_dirty_vectors, load_sqlite_vec_extension
from hl_mem.storage.tokenized_fts import ensure_tokenized_fts_v2

LOGGER = logging.getLogger(__name__)

_ACTIVE_CLAIM_GUARD_MIGRATION = "041_active_claim_guard"
_ACTIVE_CLAIM_GUARD_TRIGGERS = (
    "claims_active_exclusive_guard_insert",
    "claims_active_exclusive_guard_update",
)
_PRE_GUARD_DATA_MIGRATIONS = (
    "006_data_conflict_key_v2",
    "011_data_fact_hash_v2",
    "016_data_conflict_key_v3",
    "038_data_subject_canonicalization_v2",
)


def register_entity_sqlite_functions(connection: sqlite3.Connection) -> None:
    connection.create_function("hl_mem_normalize_alias", 1, normalize_typed_alias, deterministic=True)
    connection.create_function("hl_mem_audit_dimension", 1, current_audit_dimension)


def register_claim_mutation_audit_context(connection: sqlite3.Connection) -> None:
    """Bridge process-local audit dimensions into portable persistent triggers."""
    dimensions = (
        "trace_id",
        "tenant_id",
        "event_id",
        "related_claim_id",
        "query_id",
        "job_id",
        "claim_mutation_source",
    )
    columns = ",".join(dimensions)
    values = ",".join(f"hl_mem_audit_dimension('{name}')" for name in dimensions)
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TEMP TRIGGER IF NOT EXISTS hl_mem_claim_mutation_context_{operation.lower()} "
            f"BEFORE {operation} ON main.claims BEGIN "
            f"INSERT OR REPLACE INTO claim_mutation_audit_context(singleton,{columns}) "
            f"VALUES (1,{values}); END"
        )


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
        self._read_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=self.pool_size)
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
        register_entity_sqlite_functions(connection)
        register_claim_mutation_audit_context(connection)
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

    def _new_readonly_connection(self) -> sqlite3.Connection:
        """创建强制 query-only 的文件数据库连接。"""
        if self.path == ":memory:":
            raise ValueError("read-only connections require a file-backed database")
        if self.path.startswith("file:"):
            separator = "&" if "?" in self.path else "?"
            uri = f"{self.path}{separator}mode=ro"
        else:
            uri = f"{Path(self.path).expanduser().resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=self.busy_timeout_seconds,
            check_same_thread=False,
            factory=HLMemoryConnection,
        )
        connection.hl_mem_settings = self.settings
        connection.row_factory = sqlite3.Row
        register_entity_sqlite_functions(connection)
        connection.execute("PRAGMA query_only=ON")
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
            register_entity_sqlite_functions(connection)
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
                self._drain_dirty_vectors(connection)
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

    def open_readonly(self) -> sqlite3.Connection:
        """返回独立只读连接；调用方负责关闭或交回只读池。"""
        self._ensure_migrated()
        try:
            return self._read_pool.get_nowait()
        except queue.Empty:
            return self._new_readonly_connection()

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

    @contextmanager
    def connect_readonly(self) -> Iterator[sqlite3.Connection]:
        """获取请求级只读连接并在退出时归还独立连接池。"""
        connection = self.open_readonly()
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.rollback()
            try:
                self._read_pool.put_nowait(connection)
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
        migrations = sorted(migration_dir.glob("*.sql"))
        deferred_guard = next(
            (migration for migration in migrations if migration.stem == _ACTIVE_CLAIM_GUARD_MIGRATION),
            None,
        )
        for migration in migrations:
            if migration == deferred_guard:
                continue
            self._apply_sql_migration(connection, migration)
        guard_suspended = bool(
            deferred_guard is not None
            and connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                (_ACTIVE_CLAIM_GUARD_MIGRATION,),
            ).fetchone()
            and self._pre_guard_data_migration_pending(connection)
        )
        if guard_suspended:
            self._drop_active_claim_guards(connection)
        try:
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version='006_canonical_attribute'").fetchone():
                backfill_conflict_keys_v2(connection)
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version='011_fact_hash_v2'").fetchone():
                backfill_fact_hash_v2(connection)
            if connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version='016_claim_slots_and_tags'"
            ).fetchone():
                backfill_conflict_keys_v3(connection)
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version='036_tokenized_fts_v2'").fetchone():
                ensure_tokenized_fts_v2(connection)
            if connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version='038_subject_canonicalization'"
            ).fetchone():
                backfill_subject_canonicalization(connection)
        except Exception:
            if guard_suspended and deferred_guard is not None:
                self._apply_sql_migration(connection, deferred_guard)
            raise
        if deferred_guard is not None:
            self._apply_sql_migration(connection, deferred_guard)
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

    def _apply_sql_migration(self, connection: sqlite3.Connection, migration: Path) -> None:
        version = migration.stem
        try:
            connection.execute("BEGIN IMMEDIATE")
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            repair_legacy_vector_control = version == "037_vector_index_control" and not self._vector_control_complete(
                connection
            )
            repair_active_claim_guard = (
                version == _ACTIVE_CLAIM_GUARD_MIGRATION
                and not self._active_claim_guard_complete(connection, migration)
            )
            if already_applied and not repair_legacy_vector_control and not repair_active_claim_guard:
                connection.commit()
                return
            if repair_active_claim_guard:
                for trigger_name in _ACTIVE_CLAIM_GUARD_TRIGGERS:
                    connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            for statement in self._read_sql_statements(migration):
                connection.execute(statement)
            if not already_applied:
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _read_sql_statements(migration: Path) -> list[str]:
        statements: list[str] = []
        statement = ""
        for line in migration.read_text(encoding="utf-8").splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                statements.append(statement)
                statement = ""
        if statement.strip():
            raise sqlite3.OperationalError(f"incomplete SQL in migration {migration.stem}")
        return statements

    @staticmethod
    def _pre_guard_data_migration_pending(connection: sqlite3.Connection) -> bool:
        placeholders = ",".join("?" for _ in _PRE_GUARD_DATA_MIGRATIONS)
        applied = {
            str(row[0])
            for row in connection.execute(
                f"SELECT version FROM schema_migrations WHERE version IN ({placeholders})",
                _PRE_GUARD_DATA_MIGRATIONS,
            ).fetchall()
        }
        return len(applied) != len(_PRE_GUARD_DATA_MIGRATIONS)

    @staticmethod
    def _drop_active_claim_guards(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for trigger_name in _ACTIVE_CLAIM_GUARD_TRIGGERS:
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _active_claim_guard_complete(connection: sqlite3.Connection, migration: Path) -> bool:
        expected: dict[str, str] = {}
        for statement in Database._read_sql_statements(migration):
            normalized = Database._normalize_trigger_sql(statement)
            for trigger_name in _ACTIVE_CLAIM_GUARD_TRIGGERS:
                if f"trigger {trigger_name}" in normalized:
                    expected[trigger_name] = normalized
        placeholders = ",".join("?" for _ in _ACTIVE_CLAIM_GUARD_TRIGGERS)
        installed = {
            str(row[0]): Database._normalize_trigger_sql(str(row[1]))
            for row in connection.execute(
                f"SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})",
                _ACTIVE_CLAIM_GUARD_TRIGGERS,
            ).fetchall()
        }
        return installed == expected

    @staticmethod
    def _normalize_trigger_sql(sql: str) -> str:
        normalized = " ".join(sql.split()).rstrip(";").casefold()
        return normalized.replace("create trigger if not exists ", "create trigger ", 1)

    @staticmethod
    def _vector_control_complete(connection: sqlite3.Connection) -> bool:
        """识别旧 Python 037 已登记但缺少 SQL trigger 的过渡状态。"""
        objects = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE "
                "(type='table' AND name IN ('vector_index_state','claim_vector_dirty')) OR "
                "(type='trigger' AND name IN "
                "('claim_vector_dirty_ai','claim_vector_dirty_au','claim_vector_dirty_ad'))"
            ).fetchall()
        }
        return objects == {
            "vector_index_state",
            "claim_vector_dirty",
            "claim_vector_dirty_ai",
            "claim_vector_dirty_au",
            "claim_vector_dirty_ad",
        }

    def _drain_dirty_vectors(self, connection: sqlite3.Connection) -> None:
        """在 sqlite-vec 启动完成后修复旁路 SQL 留下的派生投影。"""
        if VectorBackend(self.settings.vector_backend) is not VectorBackend.SQLITE_VEC:
            return
        if connection.execute("SELECT 1 FROM claim_vector_dirty LIMIT 1").fetchone() is None:
            return
        from hl_mem.storage.claims import ClaimRepository

        repository = ClaimRepository(connection, settings=self.settings)
        synced, deleted, remaining = drain_dirty_vectors(
            connection,
            sync_vector=repository.sync_vector,
            delete_vector=repository.delete_vector,
        )
        LOGGER.info(
            "sqlite_vec dirty drain completed: synced=%d deleted=%d remaining=%d",
            synced,
            deleted,
            remaining,
        )

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
        while True:
            try:
                self._read_pool.get_nowait()
            except queue.Empty:
                break
        for connection in connections:
            connection.close()
