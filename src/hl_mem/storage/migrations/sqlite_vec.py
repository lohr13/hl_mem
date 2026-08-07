"""按配置启用的 sqlite-vec vec0 provisioning、回填与版本检查。"""

from __future__ import annotations

import math
import sqlite3
import struct

from hl_mem.errors import ConfigurationError

VECTOR_BACKEND_NAME = "sqlite_vec"
VECTOR_SCHEMA_VERSION = 1
VECTOR_TABLE = "claims_vec_v1"


def _started_transaction(connection: sqlite3.Connection) -> bool:
    if connection.in_transaction:
        return False
    connection.execute("BEGIN IMMEDIATE")
    return True


def disable_sqlite_vec(connection: sqlite3.Connection) -> None:
    """通过控制状态关闭 dirty trigger 守卫；scan-only 连接无需加载扩展。"""
    started_transaction = _started_transaction(connection)
    connection.execute(
        "UPDATE vector_index_state SET enabled=0,last_checked_at=CURRENT_TIMESTAMP WHERE backend=?",
        (VECTOR_BACKEND_NAME,),
    )
    if started_transaction:
        connection.commit()


def embedding_is_indexable(blob: object, embedding_dim: int) -> bool:
    """检查 vec0 可表示的有限、非零 float32 BLOB。"""
    if not isinstance(blob, bytes | bytearray | memoryview):
        return False
    materialized = bytes(blob)
    if len(materialized) != embedding_dim * 4:
        return False
    values = (value[0] for value in struct.iter_unpack("<f", materialized))
    squared_norm = 0.0
    for value in values:
        if not math.isfinite(value):
            return False
        squared_norm += value * value
    return squared_norm > 0.0


def _upsert_dirty(connection: sqlite3.Connection, claim_id: str, reason: str) -> None:
    connection.execute(
        "INSERT INTO claim_vector_dirty(claim_id,reason,queued_at) VALUES(?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(claim_id) DO UPDATE SET reason=excluded.reason,queued_at=CURRENT_TIMESTAMP",
        (claim_id, reason),
    )


def _existing_vector_schema(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (VECTOR_TABLE,)).fetchone()
    return str(row[0]) if row and row[0] else None


def migrate_sqlite_vec(
    connection: sqlite3.Connection,
    embedding_dim: int,
    embedding_model: str,
    extension_version: str,
) -> None:
    """幂等创建 vec0 并回填存量投影；不修改权威 Claim。"""
    if embedding_dim < 1:
        raise ConfigurationError("sqlite-vec embedding dimension must be positive")
    if not embedding_model.strip():
        raise ConfigurationError("sqlite-vec embedding model must not be empty")
    state = connection.execute(
        "SELECT * FROM vector_index_state WHERE backend=?",
        (VECTOR_BACKEND_NAME,),
    ).fetchone()
    schema = _existing_vector_schema(connection)
    if state is not None and schema is not None:
        state_values = (
            dict(state)
            if isinstance(state, sqlite3.Row)
            else {
                "enabled": state[1],
                "schema_version": state[2],
                "build_status": state[3],
                "embedding_model": state[4],
                "embedding_dim": state[5],
                "extension_version": state[6],
            }
        )
        compatible = (
            int(state_values["schema_version"]) == VECTOR_SCHEMA_VERSION
            and int(state_values["embedding_dim"]) == embedding_dim
            and state_values["embedding_model"] == embedding_model
            and state_values["extension_version"] == extension_version
            and f"FLOAT[{embedding_dim}]" in schema.upper()
        )
        if not compatible:
            started_transaction = _started_transaction(connection)
            try:
                connection.execute(
                    "UPDATE vector_index_state SET enabled=0,build_status='rebuild_required',"
                    "last_error='sqlite-vec schema/model/dimension mismatch',last_checked_at=CURRENT_TIMESTAMP "
                    "WHERE backend=?",
                    (VECTOR_BACKEND_NAME,),
                )
                if started_transaction:
                    connection.commit()
            except Exception:
                if started_transaction and connection.in_transaction:
                    connection.rollback()
                raise
            raise ConfigurationError(
                "sqlite-vec index schema, extension version, embedding model, or dimension does not match; "
                "rebuild the derived vector index or select sqlite_scan"
            )
        if int(state_values["enabled"]) == 1 and state_values["build_status"] in {"ready", "degraded"}:
            return

    started_transaction = _started_transaction(connection)
    try:
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0("
            "claim_id TEXT PRIMARY KEY, namespace_key TEXT PARTITION KEY, "
            f"embedding FLOAT[{embedding_dim}] DISTANCE_METRIC=cosine)"
        )
        connection.execute(
            "INSERT INTO vector_index_state("
            "backend,enabled,schema_version,build_status,embedding_model,embedding_dim,"
            "extension_version,started_at,last_checked_at,last_error) "
            "VALUES(?,1,?,'building',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL) "
            "ON CONFLICT(backend) DO UPDATE SET enabled=1,schema_version=excluded.schema_version,"
            "build_status='building',embedding_model=excluded.embedding_model,"
            "embedding_dim=excluded.embedding_dim,extension_version=excluded.extension_version,"
            "started_at=CURRENT_TIMESTAMP,last_checked_at=CURRENT_TIMESTAMP,last_error=NULL",
            (VECTOR_BACKEND_NAME, VECTOR_SCHEMA_VERSION, embedding_model, embedding_dim, extension_version),
        )
        connection.execute(f"DELETE FROM {VECTOR_TABLE}")
        connection.execute("DELETE FROM claim_vector_dirty")
        rows = connection.execute(
            "SELECT id,namespace_key,embedding_dense,embedding_model,embedding_dim "
            "FROM claims WHERE embedding_dense IS NOT NULL ORDER BY id"
        ).fetchall()
        invalid_count = 0
        for row in rows:
            claim_id = str(row["id"] if isinstance(row, sqlite3.Row) else row[0])
            namespace = str(row["namespace_key"] if isinstance(row, sqlite3.Row) else row[1])
            embedding = row["embedding_dense"] if isinstance(row, sqlite3.Row) else row[2]
            model = row["embedding_model"] if isinstance(row, sqlite3.Row) else row[3]
            dimension = row["embedding_dim"] if isinstance(row, sqlite3.Row) else row[4]
            if (
                model != embedding_model
                or dimension != embedding_dim
                or not embedding_is_indexable(embedding, embedding_dim)
            ):
                _upsert_dirty(connection, claim_id, "invalid_embedding")
                invalid_count += 1
                continue
            connection.execute(
                f"INSERT INTO {VECTOR_TABLE}(claim_id,namespace_key,embedding) VALUES(?,?,?)",
                (claim_id, namespace, bytes(embedding)),
            )
        status = "degraded" if invalid_count else "ready"
        last_error = f"{invalid_count} claim embedding(s) are not indexable" if invalid_count else None
        connection.execute(
            "UPDATE vector_index_state SET build_status=?,ready_at=CURRENT_TIMESTAMP,"
            "last_checked_at=CURRENT_TIMESTAMP,last_error=? WHERE backend=?",
            (status, last_error, VECTOR_BACKEND_NAME),
        )
        if started_transaction:
            connection.commit()
    except Exception:
        if started_transaction and connection.in_transaction:
            connection.rollback()
        raise
