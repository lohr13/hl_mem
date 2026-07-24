"""声明仓储。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hl_mem.config import RECALL_DEFAULT_LIMIT, RECALL_VECTOR_SCAN_LIMIT
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.entity import normalize_entity_id
from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.domain.types import StoredClaim
from hl_mem.errors import ValidationError
from hl_mem.lifecycle import ClaimStatus, assert_transition


@dataclass(frozen=True)
class SupersedeResult:
    """原子替代操作结果。"""

    applied: bool


from hl_mem.storage._shared import (
    decode_json,
    encode_json,
    insert_row,
    is_fts_syntax_error,
    row_to_dict,
    sanitize_fts_query,
)


class ClaimRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_claim(self, claim: dict[str, Any], commit: bool = True) -> bool:
        stored = dict(claim)
        if "value" in stored:
            stored["value_json"] = encode_json(stored.pop("value"), sort_keys=True)
        if "qualifiers" in stored:
            stored["qualifiers_json"] = encode_json(stored.pop("qualifiers"), sort_keys=True)
        return insert_row(self.connection, "claims", stored, commit)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        return self._decode_claim(
            row_to_dict(self.connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
        )

    def get_stored_claim(self, claim_id: str) -> StoredClaim | None:
        """按稳定领域类型返回声明；旧 get_claim 字典接口继续兼容。"""
        claim = self.get_claim(claim_id)
        if claim is None:
            return None
        return StoredClaim(
            id=claim["id"],
            entity_id=claim.get("subject_entity_id") or "",
            predicate=claim["predicate"],
            canonical_attribute=claim["canonical_attribute"],
            value=str(claim.get("value", "")),
            scope=claim["scope"],
            importance=float(claim["importance"]),
            status=claim["status"],
            qualifiers=claim.get("qualifiers") or {},
            created_at=claim["recorded_from"],
            valid_from=claim["valid_from"],
            valid_until=claim.get("valid_to"),
        )

    def batch_get_claims(self, claim_ids: list[str]) -> dict[str, dict[str, Any]]:
        """批量获取多个 claim，并将单次查询限制在 500 个标识以内。"""
        unique_ids = list(dict.fromkeys(claim_ids))
        if not unique_ids:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 500):
            chunk = unique_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT * FROM claims WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                claim = self._decode_claim(dict(row))
                assert claim is not None
                result[claim["id"]] = claim
        return result

    def update_status(self, claim_id: str, status: str, commit: bool = True) -> bool:
        try:
            ClaimStatus(status)
        except ValueError as error:
            raise ValidationError(f"invalid claim status: {status}") from error
        cursor = self.connection.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id))
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

    def find_active(self, namespace: str, subject_entity_id: str | None) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND subject_entity_id IS ? " "AND status='active'",
            (namespace, subject_entity_id),
        ).fetchall()
        return self._decode_rows(rows)

    def find_active_for_dedup(self, namespace: str, normalized_subject: str) -> list[dict[str, Any]]:
        """返回 namespace 内实体归一化后匹配的可去重候选。"""
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? " "AND status IN ('active','candidate','disputed')",
            (namespace,),
        ).fetchall()
        return [
            claim
            for claim in self._decode_rows(rows)
            if normalize_entity_id(claim.get("subject_entity_id")) == normalized_subject
        ]

    def find_by_conflict_key(self, conflict_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE conflict_key=? AND status IN ('active','candidate','disputed') "
            "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'disputed' THEN 1 WHEN 'candidate' THEN 2 END, "
            "valid_from DESC,recorded_from DESC,id DESC",
            (conflict_key,),
        ).fetchall()
        return self._decode_rows(rows)

    def find_by_fact_hash(self, namespace: str, fact_hash: str) -> dict[str, Any] | None:
        return self._decode_claim(
            row_to_dict(
                self.connection.execute(
                    "SELECT * FROM claims WHERE namespace_key=? AND fact_hash=? "
                    "AND status IN ('active','candidate','disputed') ORDER BY recorded_from DESC LIMIT 1",
                    (namespace, fact_hash),
                ).fetchone()
            )
        )

    def list_embedded(
        self,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or (RecallIntent.HISTORICAL if as_of else RecallIntent.CURRENT_STATE))
        statuses = "('active','superseded','expired')" if selected_intent is RecallIntent.HISTORICAL else "('active')"
        rows = self.connection.execute(
            f"SELECT * FROM claims WHERE embedding_dense IS NOT NULL AND status IN {statuses} "
            "AND namespace_key=? "
            "AND (valid_from IS NULL OR valid_from<=?) AND (valid_to IS NULL OR valid_to>?)",
            (namespace, reference, reference),
        ).fetchall()
        return [
            claim
            for claim in self._decode_rows(rows)
            if claim_is_visible(claim, reference, known_as_of, selected_intent)
        ]

    def search_claims_vector(
        self,
        query_blob: bytes,
        limit: int = RECALL_VECTOR_SCAN_LIMIT,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        # A 100k x 2048 float32 full scan is about 819 MB; indexed retrieval must
        # be reconsidered before deployments approach that scale.
        return sorted(
            self.list_embedded(as_of, intent, known_as_of, namespace),
            key=lambda claim: cosine_similarity(query_blob, claim["embedding_dense"]),
            reverse=True,
        )[:limit]

    def record_access(self, claim_ids: list[str], accessed_at: str) -> int:
        unique_ids = list(dict.fromkeys(claim_ids))
        total = 0
        try:
            for start in range(0, len(unique_ids), 500):
                chunk = unique_ids[start : start + 500]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                cursor = self.connection.execute(
                    "UPDATE claims SET access_count=access_count+1,last_accessed_at=? "
                    f"WHERE id IN ({placeholders}) "
                    "AND status IN ('active','disputed','superseded')",
                    (accessed_at, *chunk),
                )
                total += cursor.rowcount
            self.connection.commit()
            return total
        except Exception:
            self.connection.rollback()
            raise

    def helpful_rates(self, claim_ids: list[str]) -> dict[str, float]:
        """返回已有显式反馈的 claim helpful 比率。"""
        unique_ids = list(dict.fromkeys(claim_ids))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.connection.execute(
            "SELECT memory_id,avg(helpful) AS helpful_rate FROM retrieval_feedback "
            f"WHERE memory_type='claim' AND helpful IS NOT NULL AND memory_id IN ({placeholders}) "
            "GROUP BY memory_id",
            unique_ids,
        ).fetchall()
        return {row["memory_id"]: float(row["helpful_rate"]) for row in rows}

    def insert_conflict_case(self, conflict_case: dict[str, Any], commit: bool = True) -> bool:
        """写入幂等冲突审核记录。"""
        return insert_row(self.connection, "conflict_cases", conflict_case, commit)

    def find_disputed_rivals(self, conflict_keys: list[str], namespace: str) -> dict[str, list[dict[str, Any]]]:
        """批量返回同命名空间内按冲突键分组的 disputed 声明。"""
        unique_keys = list(dict.fromkeys(conflict_keys))
        result: dict[str, list[dict[str, Any]]] = {key: [] for key in unique_keys}
        for start in range(0, len(unique_keys), 500):
            chunk = unique_keys[start : start + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT id,value_json,conflict_key FROM claims "
                f"WHERE conflict_key IN ({placeholders}) AND status='disputed' AND namespace_key=?",
                (*chunk, namespace),
            ).fetchall()
            for row in rows:
                result[row["conflict_key"]].append({"id": row["id"], "value": decode_json(row["value_json"])})
        return result

    @staticmethod
    def _decode_claim(claim: dict[str, Any] | None) -> dict[str, Any] | None:
        """在仓储边界为兼容字典附加已解码的 Python 值。"""
        if claim is None:
            return None
        if "value_json" in claim:
            claim["value"] = decode_json(claim["value_json"])
        if "qualifiers_json" in claim:
            claim["qualifiers"] = decode_json(claim["qualifiers_json"])
        return claim

    @classmethod
    def _decode_rows(cls, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """批量解码 SQLite 声明行。"""
        decoded: list[dict[str, Any]] = []
        for row in rows:
            claim = cls._decode_claim(dict(row))
            if claim is not None:
                decoded.append(claim)
        return decoded

    def supersede(self, old_id: str, new_valid_from: str, commit: bool = True) -> None:
        self.connection.execute(
            "UPDATE claims SET status='superseded',valid_to=?,recorded_to=? WHERE id=?",
            (new_valid_from, new_valid_from, old_id),
        )
        if commit:
            self.connection.commit()

    def supersede_with_inline(
        self,
        old_id: str,
        new_claim_id: str,
        new_value: Any,
        changed_at: str,
        recorded_at: str,
        commit: bool = True,
    ) -> SupersedeResult:
        """以 compare-and-set 方式内联旧值并建立替代证据。"""
        if old_id == new_claim_id:
            raise ValueError("a claim cannot supersede itself")
        started_transaction = commit and not self.connection.in_transaction
        if started_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            old = self.connection.execute("SELECT * FROM claims WHERE id=?", (old_id,)).fetchone()
            if not old:
                raise ValueError(f"claim not found: {old_id}")
            if old["status"] == "superseded" and old["superseded_by_id"] == new_claim_id:
                if started_transaction:
                    self.connection.commit()
                return SupersedeResult(False)
            if old["status"] == "active":
                assert_transition(old["status"], "superseded")
            elif old["status"] not in {"candidate", "disputed"}:
                if started_transaction:
                    self.connection.rollback()
                return SupersedeResult(False)
            decoded = decode_json(old["value_json"])
            old_value = (
                decoded.get("old_value")
                if isinstance(decoded, dict) and decoded.get("_type") == "superseded_value"
                else decoded
            )
            envelope = encode_json(
                {
                    "_type": "superseded_value",
                    "schema_version": 1,
                    "old_value": old_value,
                    "new_value": new_value,
                    "superseded_by_id": new_claim_id,
                    "changed_at": changed_at,
                },
                sort_keys=True,
            )
            cursor = self.connection.execute(
                "UPDATE claims SET status='superseded',valid_to=?,recorded_to=?,value_json=?,"
                "superseded_by_id=? WHERE id=? AND status=?",
                (changed_at, recorded_at, envelope, new_claim_id, old_id, old["status"]),
            )
            if cursor.rowcount:
                self.connection.execute(
                    "INSERT OR IGNORE INTO evidence_links(id,derived_type,derived_id,evidence_type,"
                    "evidence_id,relation,weight) VALUES (lower(hex(randomblob(16))),'claim',?,'claim',"
                    "?,'supersedes',1.0)",
                    (new_claim_id, old_id),
                )
            if started_transaction:
                self.connection.commit()
            return SupersedeResult(cursor.rowcount == 1)
        except Exception:
            if started_transaction:
                self.connection.rollback()
            raise

    def search_visible(
        self,
        query: str | None,
        query_blob: bytes | None,
        limit: int,
        intent: RecallIntent,
        valid_as_of: str,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        """使用统一策略返回 FTS 或向量候选。"""
        candidates = (
            self.search_claims_fts(query, limit, valid_as_of, intent, known_as_of, namespace)
            if query is not None
            else self.search_claims_vector(query_blob or b"", limit, valid_as_of, intent, known_as_of, namespace)
        )
        return [item for item in candidates if claim_is_visible(item, valid_as_of, known_as_of, intent)]

    def retract(self, claim_id: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE claims SET status='retracted',embedding_dense=NULL,embedding_sparse=NULL WHERE id=?",
            (claim_id,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def search_claims_fts(
        self,
        query: str,
        limit: int = RECALL_DEFAULT_LIMIT,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or (RecallIntent.HISTORICAL if as_of else RecallIntent.CURRENT_STATE))
        statuses = "('active','superseded','expired')" if selected_intent is RecallIntent.HISTORICAL else "('active')"
        try:
            rows = self.connection.execute(
                "SELECT c.* FROM claims_fts f JOIN claims c ON c.rowid=f.rowid "
                f"WHERE claims_fts MATCH ? AND c.status IN {statuses} "
                "AND c.namespace_key=? "
                "AND (c.valid_from IS NULL OR c.valid_from<=?) "
                "AND (c.valid_to IS NULL OR c.valid_to>?) "
                "ORDER BY bm25(claims_fts) LIMIT ?",
                (sanitize_fts_query(query), namespace, reference, reference, limit),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if not is_fts_syntax_error(error):
                raise
            return []
        return [
            claim
            for claim in self._decode_rows(rows)
            if claim_is_visible(claim, reference, known_as_of, selected_intent)
        ]
