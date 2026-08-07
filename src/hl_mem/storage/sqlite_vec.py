"""可选 sqlite-vec 精确 KNN 后端与 Claim 向量投影同步。"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from typing import Any, cast

from hl_mem.domain.temporal import RecallIntent
from hl_mem.errors import ConfigurationError
from hl_mem.protocols import ClaimRow
from hl_mem.storage._shared import decode_json
from hl_mem.storage.candidate_materializer import materialize_candidates
from hl_mem.storage.migrations.sqlite_vec import (
    VECTOR_BACKEND_NAME,
    VECTOR_TABLE,
    embedding_is_indexable,
    migrate_sqlite_vec,
)

ScanFallback = Callable[
    [bytes, int, str, RecallIntent, str | None, str],
    list[ClaimRow] | list[dict[str, Any]],
]
VectorMutation = Callable[[str], None]
_OVERSAMPLE_FACTORS = (3, 6, 12)
_DEFAULT_MAX_PROBE = 2400


def _supported_extension_version(version: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return False
    major, minor, patch = (int(part) for part in match.groups())
    return major == 0 and minor == 1 and patch >= 9


def load_sqlite_vec_extension(connection: sqlite3.Connection) -> str:
    """在单条 connection 上加载受支持的 sqlite-vec，并恢复禁用加载权限。"""
    try:
        existing = connection.execute("SELECT vec_version()").fetchone()
    except sqlite3.Error:
        existing = None
    if existing is not None:
        version = str(existing[0])
        if not _supported_extension_version(version):
            raise ConfigurationError(
                f"unsupported sqlite-vec extension version {version!r}; require >=0.1.9,<0.2 or select sqlite_scan"
            )
        return version
    if sqlite3.sqlite_version_info < (3, 41, 0):
        raise ConfigurationError(
            f"sqlite-vec requires SQLite >=3.41, current runtime is {sqlite3.sqlite_version}; select sqlite_scan"
        )
    if not hasattr(connection, "enable_load_extension"):
        raise ConfigurationError(
            "sqlite-vec backend requested but this Python sqlite3 build cannot load extensions; select sqlite_scan"
        )
    try:
        import sqlite_vec
    except ImportError as error:
        raise ConfigurationError(
            "sqlite-vec backend requested but optional dependency 'sqlite-vec' is not installed; "
            "install hl-mem[sqlite-vec] or select sqlite_scan"
        ) from error
    try:
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
    except Exception as error:
        raise ConfigurationError(
            f"failed to load sqlite-vec native extension: {error}; "
            "install a supported sqlite-vec wheel or select sqlite_scan"
        ) from error
    try:
        version = str(connection.execute("SELECT vec_version()").fetchone()[0])
    except sqlite3.Error as error:
        raise ConfigurationError(f"sqlite-vec loaded but vec_version() failed: {error}") from error
    if not _supported_extension_version(version):
        raise ConfigurationError(
            f"unsupported sqlite-vec extension version {version!r}; require >=0.1.9,<0.2 or select sqlite_scan"
        )
    return version


def _decode_claim(row: sqlite3.Row) -> dict[str, Any]:
    claim = dict(row)
    if "value_json" in claim:
        claim["value"] = decode_json(claim.pop("value_json"))
    if "qualifiers_json" in claim:
        claim["qualifiers"] = decode_json(claim.pop("qualifiers_json"))
    if "topic_tags_json" in claim:
        claim["topic_tags"] = decode_json(claim.pop("topic_tags_json") or "[]")
    if "entities_json" in claim:
        encoded_entities = claim.pop("entities_json")
        claim["entities"] = decode_json(encoded_entities) if encoded_entities else None
    return claim


def drain_dirty_vectors(
    connection: sqlite3.Connection,
    *,
    sync_vector: VectorMutation,
    delete_vector: VectorMutation,
) -> tuple[int, int, int]:
    """事务内修复 dirty Claim 投影，返回同步、删除和未解决数量。"""
    dirty_rows = connection.execute("SELECT claim_id FROM claim_vector_dirty ORDER BY claim_id").fetchall()
    if not dirty_rows:
        return 0, 0, 0
    started_transaction = not connection.in_transaction
    if started_transaction:
        connection.execute("BEGIN IMMEDIATE")
    synced = 0
    deleted = 0
    try:
        for dirty_row in dirty_rows:
            claim_id = str(dirty_row["claim_id"] if isinstance(dirty_row, sqlite3.Row) else dirty_row[0])
            claim = connection.execute("SELECT embedding_dense FROM claims WHERE id=?", (claim_id,)).fetchone()
            embedding = claim["embedding_dense"] if isinstance(claim, sqlite3.Row) else claim[0] if claim else None
            if claim is not None and embedding is not None:
                sync_vector(claim_id)
                synced += 1
            else:
                delete_vector(claim_id)
                deleted += 1
        remaining = int(connection.execute("SELECT COUNT(*) FROM claim_vector_dirty").fetchone()[0])
        if started_transaction:
            connection.commit()
        return synced, deleted, remaining
    except Exception:
        if started_transaction and connection.in_transaction:
            connection.rollback()
        raise


class SQLiteVecVectorBackend:
    """使用 vec0 KNN，并在 Claim 回表后执行权威可见性过滤。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        embedding_dim: int,
        embedding_model: str,
        scan_fallback: ScanFallback,
        max_probe: int = _DEFAULT_MAX_PROBE,
    ) -> None:
        if max_probe < 1:
            raise ValueError("sqlite-vec max_probe must be positive")
        self.connection = connection
        self.embedding_dim = embedding_dim
        self.embedding_model = embedding_model
        self.scan_fallback = scan_fallback
        self.max_probe = max_probe
        extension_version = load_sqlite_vec_extension(connection)
        migrate_sqlite_vec(connection, embedding_dim, embedding_model, extension_version)

    def _claim_row(self, claim_id: str) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT id,namespace_key,embedding_dense,embedding_model,embedding_dim FROM claims WHERE id=?",
            (claim_id,),
        ).fetchone()
        return cast(sqlite3.Row | None, row)

    def _mark_degraded(self, claim_id: str, reason: str) -> None:
        self.connection.execute(
            "INSERT INTO claim_vector_dirty(claim_id,reason,queued_at) VALUES(?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(claim_id) DO UPDATE SET reason=excluded.reason,queued_at=CURRENT_TIMESTAMP",
            (claim_id, reason),
        )
        self.connection.execute(
            "UPDATE vector_index_state SET build_status='degraded',last_error=?,"
            "last_checked_at=CURRENT_TIMESTAMP WHERE backend=?",
            (f"claim {claim_id}: {reason}", VECTOR_BACKEND_NAME),
        )

    def _mark_ready_if_clean(self) -> None:
        self.connection.execute(
            "UPDATE vector_index_state SET build_status='ready',last_error=NULL,"
            "last_checked_at=CURRENT_TIMESTAMP WHERE backend=? AND enabled=1 "
            "AND build_status='degraded' AND NOT EXISTS(SELECT 1 FROM claim_vector_dirty)",
            (VECTOR_BACKEND_NAME,),
        )

    def _replace(self, claim_id: str) -> None:
        row = self._claim_row(claim_id)
        self.connection.execute(f"DELETE FROM {VECTOR_TABLE} WHERE claim_id=?", (claim_id,))
        if row is None or row["embedding_dense"] is None:
            self.connection.execute("DELETE FROM claim_vector_dirty WHERE claim_id=?", (claim_id,))
            self._mark_ready_if_clean()
            return
        embedding = row["embedding_dense"]
        if (
            row["embedding_model"] != self.embedding_model
            or row["embedding_dim"] != self.embedding_dim
            or not embedding_is_indexable(embedding, self.embedding_dim)
        ):
            self._mark_degraded(claim_id, "invalid_embedding")
            return
        self.connection.execute(
            f"INSERT INTO {VECTOR_TABLE}(claim_id,namespace_key,embedding) VALUES(?,?,?)",
            (claim_id, str(row["namespace_key"]), bytes(embedding)),
        )
        self.connection.execute("DELETE FROM claim_vector_dirty WHERE claim_id=?", (claim_id,))
        self._mark_ready_if_clean()

    def insert(self, claim_id: str) -> None:
        """同步新 Claim；重复调用保持幂等。"""
        self._replace(claim_id)

    def delete(self, claim_id: str) -> None:
        """删除派生向量并清理 dirty 探针。"""
        self.connection.execute(f"DELETE FROM {VECTOR_TABLE} WHERE claim_id=?", (claim_id,))
        self.connection.execute("DELETE FROM claim_vector_dirty WHERE claim_id=?", (claim_id,))
        self._mark_ready_if_clean()

    def update(self, claim_id: str) -> None:
        """稳定版统一以 DELETE+INSERT 同步 embedding 或 namespace 变化。"""
        self._replace(claim_id)

    def _fallback(
        self,
        query_blob: bytes,
        limit: int,
        reference_time: str,
        intent: RecallIntent,
        known_as_of: str | None,
        namespace: str,
    ) -> list[ClaimRow]:
        return cast(
            list[ClaimRow],
            self.scan_fallback(query_blob, limit, reference_time, intent, known_as_of, namespace),
        )

    def _state_is_ready(self) -> bool:
        row = self.connection.execute(
            "SELECT enabled,build_status FROM vector_index_state WHERE backend=?",
            (VECTOR_BACKEND_NAME,),
        ).fetchone()
        return bool(row and int(row["enabled"]) == 1 and row["build_status"] == "ready")

    def _has_dirty_rows(self) -> bool:
        return self.connection.execute("SELECT 1 FROM claim_vector_dirty LIMIT 1").fetchone() is not None

    def batch_get_claims(self, claim_ids: list[str]) -> dict[str, dict[str, Any]]:
        """批量加载 vec0 候选对应的权威 Claim。"""
        if not claim_ids:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(claim_ids), 500):
            chunk = claim_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM claims WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                claim = _decode_claim(row)
                result[str(claim["id"])] = claim
        return result

    def search(
        self,
        query_blob: bytes,
        limit: int,
        reference_time: str,
        intent: RecallIntent,
        known_as_of: str | None,
        namespace: str,
    ) -> list[ClaimRow]:
        """执行 3x→6x→12x KNN；无法证明完整时回退 sqlite scan。"""
        if limit <= 0:
            return []
        started_transaction = not self.connection.in_transaction
        if started_transaction:
            self.connection.execute("BEGIN")
        try:
            return self._search_snapshot(
                query_blob,
                limit,
                reference_time,
                intent,
                known_as_of,
                namespace,
            )
        finally:
            if started_transaction and self.connection.in_transaction:
                self.connection.rollback()

    def _search_snapshot(
        self,
        query_blob: bytes,
        limit: int,
        reference_time: str,
        intent: RecallIntent,
        known_as_of: str | None,
        namespace: str,
    ) -> list[ClaimRow]:
        """在调用方或本方法建立的单一 SQLite snapshot 内完成检索。"""
        if (
            not self._state_is_ready()
            or self._has_dirty_rows()
            or not embedding_is_indexable(query_blob, self.embedding_dim)
        ):
            return self._fallback(query_blob, limit, reference_time, intent, known_as_of, namespace)
        namespace_count = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM {VECTOR_TABLE} WHERE namespace_key=?",
                (namespace,),
            ).fetchone()[0]
        )
        if namespace_count == 0:
            return []
        last_probe = 0
        for factor in _OVERSAMPLE_FACTORS:
            probe_k = min(namespace_count, limit * factor, self.max_probe)
            if probe_k <= last_probe:
                continue
            last_probe = probe_k
            knn_rows = self.connection.execute(
                f"SELECT claim_id,distance FROM {VECTOR_TABLE} "
                "WHERE embedding MATCH ? AND k=? AND namespace_key=? ORDER BY distance",
                (query_blob, probe_k, namespace),
            ).fetchall()
            if any(row["distance"] is None for row in knn_rows):
                return self._fallback(query_blob, limit, reference_time, intent, known_as_of, namespace)
            ordered = sorted(
                ((str(row["claim_id"]), float(row["distance"])) for row in knn_rows),
                key=lambda item: (item[1], item[0]),
            )
            visible = materialize_candidates(
                self,
                [(claim_id, 1.0 - distance) for claim_id, distance in ordered],
                limit,
                reference_time,
                known_as_of,
                intent,
                claim_filter=lambda claim: (
                    claim.get("namespace_key") == namespace and claim.get("embedding_dense") is not None
                ),
            )
            if len(visible) >= limit:
                return cast(list[ClaimRow], visible)
            if probe_k >= namespace_count:
                return cast(list[ClaimRow], visible)
        return self._fallback(query_blob, limit, reference_time, intent, known_as_of, namespace)
