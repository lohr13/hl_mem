"""声明仓储。"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence, cast

from hl_mem.core.vector import batch_cosine_similarity, cosine_similarity
from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.claims.claim import build_index_text
from hl_mem.domain.claims.conflicts import (
    compute_conflict_group_case_key,
    compute_conflict_group_key,
    slot_qualifier_key,
)
from hl_mem.domain.temporal import RecallIntent, claim_is_visible
from hl_mem.errors import ActiveClaimInvariantError, ConflictError, ValidationError
from hl_mem.lifecycle import ClaimStatus, assert_transition
from hl_mem.protocols import ClaimRow, EmbedderProtocol
from hl_mem.recall.lexicalizer import prepare_fts_document, prepare_fts_query
from hl_mem.settings import Settings, VectorBackend
from hl_mem.storage._shared import (
    decode_json,
    encode_json,
    insert_row,
    is_fts_syntax_error,
    row_to_dict,
)
from hl_mem.storage.candidate_materializer import materialize_candidates
from hl_mem.storage.sqlite_vec import SQLiteVecVectorBackend

_CURRENT_STATUS_SQL = "('active','superseded','expired')"
_HISTORICAL_STATUS_SQL = "('active','archived','superseded','expired')"
_REKEY_IDENTITY_FIELDS = "id namespace_key status subject_entity_id canonical_slot".split()


def _recall_statuses_sql(intent: RecallIntent) -> str:
    return _HISTORICAL_STATUS_SQL if intent is RecallIntent.HISTORICAL else _CURRENT_STATUS_SQL


def _valid_time_sql(intent: RecallIntent, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    started = f"AND ({prefix}valid_from IS NULL OR {prefix}valid_from<=?) "
    if intent is RecallIntent.HISTORICAL:
        return started
    return started + f"AND ({prefix}valid_to IS NULL OR {prefix}valid_to>?) "


def _valid_time_parameters(intent: RecallIntent, reference: str) -> tuple[str, ...]:
    return (reference,) if intent is RecallIntent.HISTORICAL else (reference, reference)


@dataclass(frozen=True)
class SupersedeResult:
    """原子替代操作结果。"""

    applied: bool


@dataclass(frozen=True)
class ConflictGroupLifecycle:
    """Latest persisted lifecycle facts for one group-native conflict key."""

    latest_generation: int
    latest_terminal_generation: int | None
    open_case_id: str | None


class ClaimRepository:
    """封装 Claim 持久化、时间可见检索、状态更新与去重查询。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        vector_batch_size: int = 512,
        settings: Settings | None = None,
    ) -> None:
        self.connection = connection
        if vector_batch_size < 1:
            raise ValueError("vector_batch_size must be positive")
        self.vector_batch_size = vector_batch_size
        resolved_settings = settings or getattr(connection, "hl_mem_settings", None) or Settings()
        self.recall_default_limit = resolved_settings.recall_default_limit
        self.recall_vector_scan_limit = resolved_settings.recall_vector_scan_limit
        self.index_text_mode = resolved_settings.index_text_mode
        self.fts_language = resolved_settings.fts_language
        self.conflict_auto_resolve_max_candidates = resolved_settings.conflict_auto_resolve_max_candidates
        self.vector_backend: SQLiteVecVectorBackend | None = None
        if VectorBackend(resolved_settings.vector_backend) is VectorBackend.SQLITE_VEC:
            self.vector_backend = SQLiteVecVectorBackend(
                connection,
                embedding_dim=resolved_settings.embedding_dim,
                embedding_model=resolved_settings.embedding_model,
                scan_fallback=self._search_claims_vector_scan,
            )

    def insert_claim(self, claim: dict[str, Any], commit: bool = True) -> bool:
        """编码结构化字段，并在同一事务同步写入 tokenized FTS v2。"""
        stored = dict(claim)
        if "value" in stored:
            stored["value_json"] = encode_json(stored.pop("value"), sort_keys=True)
        if "qualifiers" in stored:
            stored["qualifiers_json"] = encode_json(stored.pop("qualifiers"), sort_keys=True)
        if "index_text" not in stored:
            index_claim = dict(claim)
            if "value" not in index_claim and stored.get("value_json") is not None:
                index_claim["value"] = decode_json(stored["value_json"])
            if "topic_tags" not in index_claim and stored.get("topic_tags_json") is not None:
                index_claim["topic_tags"] = decode_json(stored["topic_tags_json"])
            stored["index_text"] = build_index_text(index_claim, mode=self.index_text_mode)
        try:
            created = insert_row(self.connection, "claims", stored, commit=False)
            if created:
                row = self.connection.execute(
                    "SELECT rowid,index_text,topic_tags_json FROM claims WHERE id=?",
                    (stored["id"],),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"inserted claim is missing: {stored['id']}")
                raw_tags = decode_json(row["topic_tags_json"] or "[]")
                if not isinstance(raw_tags, list):
                    raise ValueError(f"topic_tags_json for claim {stored['id']} must be a JSON array")
                tags_text = " ".join(dict.fromkeys(tag for tag in raw_tags if isinstance(tag, str)))
                self.connection.execute(
                    "INSERT INTO claims_fts_v2(rowid,terms) VALUES(?,?)",
                    (
                        row["rowid"],
                        prepare_fts_document(row["index_text"] or "", language=self.fts_language),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO claims_tags_fts_v2(rowid,tags_text) VALUES(?,?)",
                    (row["rowid"], tags_text),
                )
                if self.vector_backend is not None:
                    self.vector_backend.insert(str(stored["id"]))
            if commit:
                self.connection.commit()
            return created
        except Exception:
            if commit and self.connection.in_transaction:
                self.connection.rollback()
            raise

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        """按标识获取并解码 Claim，不存在时返回 None。"""
        return self._decode_claim(
            row_to_dict(self.connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone())
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
        """校验目标状态后更新 Claim 生命周期状态。"""
        try:
            ClaimStatus(status)
        except ValueError as error:
            raise ValidationError(f"invalid claim status: {status}") from error
        cursor = self.connection.execute("UPDATE claims SET status=? WHERE id=?", (status, claim_id))
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

    def find_active(self, namespace: str, subject_entity_id: str | None) -> list[dict[str, Any]]:
        """返回命名空间内指定主体的活跃 Claim。"""
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND subject_entity_id IS ? " "AND status='active'",
            (namespace, subject_entity_id),
        ).fetchall()
        return self._decode_rows(rows)

    def list_all(self) -> list[dict[str, Any]]:
        """返回全部声明，并在仓储边界完成 JSON 解码。"""
        rows = self.connection.execute("SELECT * FROM claims ORDER BY id").fetchall()
        return self._decode_rows(rows)

    def list_memories(
        self,
        namespace: str,
        status: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """按 namespace/status 返回稳定倒序的分页 Claim 与总数。"""
        if limit < 1 or offset < 0:
            raise ValueError("memory page limit must be positive and offset non-negative")
        where = "namespace_key=? AND status=?"
        parameters = (namespace, status)
        total = int(self.connection.execute(f"SELECT count(*) FROM claims WHERE {where}", parameters).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT * FROM claims WHERE {where} ORDER BY recorded_from DESC,id DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()
        return self._decode_rows(rows), total

    def list_active_for_consolidation(
        self,
        namespace: str,
        watermark: str | None,
        slot_filter: str | None = None,
        tag_filter: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """返回待归并的活跃声明，并在仓储边界完成 JSON 解码。"""
        conditions = [
            "namespace_key=?",
            "status='active'",
            "embedding_dense IS NOT NULL",
            "(? IS NULL OR recorded_from>?)",
        ]
        parameters: list[Any] = [namespace, watermark, watermark]
        if slot_filter is not None:
            conditions.append("canonical_slot=?")
            parameters.append(slot_filter)
        if tag_filter is not None:
            if not tag_filter:
                return []
            placeholders = ",".join("?" for _ in tag_filter)
            conditions.append(
                f"EXISTS (SELECT 1 FROM json_each(COALESCE(topic_tags_json, '[]')) WHERE value IN ({placeholders}))"
            )
            parameters.extend(tag_filter)
        rows = self.connection.execute(
            f"SELECT * FROM claims WHERE {' AND '.join(conditions)} ORDER BY recorded_from,id",
            parameters,
        ).fetchall()
        return self._decode_rows(rows)

    def is_unchanged(self, original: dict[str, Any]) -> bool:
        """检查声明仍活跃且 Python 值未发生变化。"""
        current = self.get_claim(original["id"])
        return bool(current and current["status"] == "active" and current.get("value") == original.get("value"))

    def update_classification(
        self,
        claim_id: str,
        scope: str,
        importance: float,
        canonical_slot: str | None,
        expires_at: str | None,
        conflict_key: str | None,
    ) -> bool:
        """原子更新声明分类、slot 生命周期及其冲突键，由调用方提交事务。"""
        if conflict_key is not None and is_mutually_exclusive_attribute(canonical_slot):
            cursor = self.connection.execute(
                "UPDATE claims SET scope=?,importance=?,canonical_slot=?,expires_at=?,conflict_key=? "
                "WHERE id=? AND (status<>'active' OR NOT EXISTS ("
                "SELECT 1 FROM claims AS other WHERE other.namespace_key=claims.namespace_key "
                "AND other.conflict_key=? AND other.status='active' AND other.id<>claims.id"
                "))",
                (
                    scope,
                    importance,
                    canonical_slot,
                    expires_at,
                    conflict_key,
                    claim_id,
                    conflict_key,
                ),
            )
            if cursor.rowcount == 0:
                exists = self.connection.execute("SELECT 1 FROM claims WHERE id=?", (claim_id,)).fetchone()
                if exists is None:
                    return False
                raise ActiveClaimInvariantError(
                    f"classification of {claim_id} would collide with active conflict group {conflict_key}"
                )
        else:
            cursor = self.connection.execute(
                "UPDATE claims SET scope=?,importance=?,canonical_slot=?,expires_at=?,conflict_key=? WHERE id=?",
                (scope, importance, canonical_slot, expires_at, conflict_key, claim_id),
            )
        return cursor.rowcount == 1

    def _cas_rekey_canonical_subject(
        self,
        expected: dict[str, Any],
        canonical_entity_id: str,
        conflict_key: str | None,
        changed_at: str,
    ) -> str:
        collision = bool(
            expected.get("status") in {"active", "candidate", "disputed"}
            and is_mutually_exclusive_attribute(expected.get("canonical_slot"))
            and conflict_key
            and self.connection.execute(
                "SELECT 1 FROM claims WHERE id<>? AND namespace_key=? AND conflict_key=? "
                "AND status IN ('active','candidate','disputed') LIMIT 1",
                (expected["id"], expected["namespace_key"], conflict_key),
            ).fetchone()
        )
        if collision and expected["status"] != "disputed":
            assert_transition(str(expected["status"]), "disputed")
        status_sql = ",status='disputed'" if collision else ""
        identity = tuple(expected.get(field) for field in _REKEY_IDENTITY_FIELDS)
        cursor = self.connection.execute(
            "UPDATE claims SET subject_canonical_entity_id=?,conflict_key=?,conflict_key_version=4"
            f"{status_sql} WHERE id=? AND namespace_key=? "
            "AND status=? AND subject_entity_id IS ? "
            "AND canonical_slot IS ? AND json(qualifiers_json)=json(?) AND subject_canonical_entity_id IS NULL "
            "AND conflict_key IS ? AND conflict_key_version=?",
            (
                canonical_entity_id,
                conflict_key,
                *identity,
                encode_json(expected.get("qualifiers") or {}, sort_keys=True),
                expected.get("conflict_key"),
                expected.get("conflict_key_version"),
            ),
        )
        if cursor.rowcount == 0:
            return "stale"
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND conflict_key=? "
            "AND status IN ('active','candidate','disputed')",
            (expected["namespace_key"], conflict_key),
        ).fetchall()
        members = self._decode_rows(rows)
        if collision:
            for member in members:
                if member["status"] in {"active", "candidate"}:
                    assert_transition(str(member["status"]), "disputed")
                    self.update_status(str(member["id"]), "disputed", commit=False)
                    member["status"] = "disputed"
            self.ensure_group_conflict_case(
                members,
                created_at=changed_at,
                decision="uncertain",
                rationale="entity_rekey_collision",
                commit=False,
            )
            return "quarantined"
        return "updated"

    def find_active_for_dedup(
        self,
        namespace: str,
        normalized_subject: str,
        canonical_slot: str,
        qualifier_key: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """按 namespace、slot 有界查询同主体和 qualifier 的去重候选。"""
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND canonical_slot=? "
            "AND subject_entity_id=? AND status IN ('active','candidate','disputed')",
            (namespace, canonical_slot, normalized_subject),
        ).fetchall()
        return [
            claim
            for claim in self._decode_rows(rows)
            if slot_qualifier_key(canonical_slot, claim.get("qualifiers")) == qualifier_key
        ]

    def find_cross_predicate_candidates(
        self,
        namespace: str,
        normalized_subject: str,
        predicate: str,
    ) -> list[dict[str, Any]]:
        """按 namespace、predicate 查询无 slot 的同主体去重候选。"""
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND canonical_slot IS NULL "
            "AND subject_entity_id=? AND predicate=? AND status IN ('active','candidate','disputed')",
            (namespace, normalized_subject, predicate),
        ).fetchall()
        return self._decode_rows(rows)

    def find_cross_subject_dedup_candidates(
        self,
        namespace: str,
        embedder: EmbedderProtocol,
        *,
        threshold: float = 0.92,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """发现同 predicate、不同 subject 的无 slot 高相似 Claim 对。"""
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("dedup threshold must be between 0 and 1")
        if limit < 1:
            raise ValueError("dedup scan limit must be positive")
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND status='active' "
            "AND canonical_slot IS NULL AND predicate IS NOT NULL "
            "AND embedding_dense IS NOT NULL ORDER BY recorded_from DESC,id DESC LIMIT ?",
            (namespace, limit),
        ).fetchall()
        del embedder  # 候选发现只使用已存向量，禁止日常扫描触发远程 embedding。
        claims = list(reversed(self._decode_rows(rows)))
        groups: dict[str, list[dict[str, Any]]] = {}
        for claim in claims:
            groups.setdefault(str(claim["predicate"]), []).append(claim)
        candidates: list[dict[str, Any]] = []
        for predicate_claims in groups.values():
            for left_index, left in enumerate(predicate_claims):
                for right in predicate_claims[left_index + 1 :]:
                    if left.get("subject_entity_id") == right.get("subject_entity_id"):
                        continue
                    similarity = cosine_similarity(left["embedding_dense"], right["embedding_dense"])
                    if similarity < threshold:
                        continue
                    candidates.append({"left": left, "right": right, "similarity": similarity})
        candidates.sort(
            key=lambda pair: (
                -pair["similarity"],
                pair["left"]["id"],
                pair["right"]["id"],
            )
        )
        return candidates

    def find_by_conflict_key(self, conflict_key: str | None) -> list[dict[str, Any]]:
        """按冲突键返回仍参与解析的候选 Claim。"""
        if conflict_key is None:
            return []
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE conflict_key=? AND status IN ('active','candidate','disputed') "
            "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'disputed' THEN 1 WHEN 'candidate' THEN 2 END, "
            "valid_from DESC,recorded_from DESC,id DESC",
            (conflict_key,),
        ).fetchall()
        return self._decode_rows(rows)

    def conflict_group_lifecycle(self, namespace: str, conflict_key: str) -> ConflictGroupLifecycle:
        """Return bounded generation state without reopening or mutating any case."""

        group_key = compute_conflict_group_key(namespace, conflict_key)
        row = self.connection.execute(
            "SELECT COALESCE(max(generation),0) AS latest_generation,"
            "max(CASE WHEN status IN ('resolved','rejected') AND resolved_at IS NOT NULL "
            "THEN generation END) AS latest_terminal_generation,"
            "max(CASE WHEN status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL "
            "THEN id END) AS open_case_id FROM conflict_cases WHERE namespace_key=? AND group_key=?",
            (namespace, group_key),
        ).fetchone()
        assert row is not None
        return ConflictGroupLifecycle(
            latest_generation=int(row["latest_generation"]),
            latest_terminal_generation=(
                int(row["latest_terminal_generation"]) if row["latest_terminal_generation"] is not None else None
            ),
            open_case_id=str(row["open_case_id"]) if row["open_case_id"] is not None else None,
        )

    def find_temporal_candidates(self, claim: dict[str, Any], limit: int = 16) -> list[dict[str, Any]]:
        """Return a bounded active series for the conservative temporal-link evaluator."""

        if limit < 1:
            raise ValueError("temporal candidate limit must be positive")
        identity = tuple(
            claim.get(field) for field in ("namespace_key", "subject_entity_id", "predicate", "canonical_attribute")
        )
        if any(value is None for value in identity):
            return []
        rows = self.connection.execute(
            "SELECT * FROM claims WHERE namespace_key=? AND subject_entity_id=? AND predicate=? "
            "AND canonical_attribute=? AND status='active' ORDER BY valid_from DESC,recorded_from DESC,id DESC LIMIT ?",
            (*identity, limit),
        ).fetchall()
        return self._decode_rows(rows)

    def find_by_fact_hash(self, namespace: str, fact_hash: str) -> dict[str, Any] | None:
        """按命名空间与事实哈希查找最新未终结 Claim。"""
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
        """返回指定双时间视图下仍携带向量的可见 Claim。"""
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
        statuses = _recall_statuses_sql(selected_intent)
        rows = self.connection.execute(
            f"SELECT * FROM claims WHERE embedding_dense IS NOT NULL AND status IN {statuses} "
            "AND namespace_key=? "
            f"{_valid_time_sql(selected_intent)}",
            (namespace, *_valid_time_parameters(selected_intent, reference)),
        ).fetchall()
        return [
            claim
            for claim in self._decode_rows(rows)
            if claim_is_visible(claim, reference, known_as_of, selected_intent)
        ]

    def search_claims_vector(
        self,
        query_blob: bytes,
        limit: int | None = None,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        """按配置委托 sqlite-vec，默认保留本地余弦扫描。"""
        effective_limit = self.recall_vector_scan_limit if limit is None else limit
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
        if self.vector_backend is not None:
            return cast(
                list[dict[str, Any]],
                self.vector_backend.search(
                    query_blob,
                    effective_limit,
                    reference,
                    selected_intent,
                    known_as_of,
                    namespace,
                ),
            )
        return self._search_claims_vector_scan(
            query_blob,
            effective_limit,
            reference,
            selected_intent,
            known_as_of,
            namespace,
        )

    def _search_claims_vector_scan(
        self,
        query_blob: bytes,
        limit: int | None = None,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        """对可见 Claim 执行本地余弦全量扫描并截断。"""
        limit = self.recall_vector_scan_limit if limit is None else limit
        if limit <= 0:
            return []
        # A 100k x 2048 float32 full scan is about 819 MB; indexed retrieval must
        # be reconsidered before deployments approach that scale.
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
        statuses = _recall_statuses_sql(selected_intent)
        cursor = self.connection.execute(
            f"SELECT id, embedding_dense FROM claims WHERE embedding_dense IS NOT NULL AND status IN {statuses} "
            "AND namespace_key=? "
            f"{_valid_time_sql(selected_intent)}",
            (namespace, *_valid_time_parameters(selected_intent, reference)),
        )
        scored_claims: list[tuple[str, float]] = []
        while rows := cursor.fetchmany(self.vector_batch_size):
            scores = batch_cosine_similarity(
                query_blob,
                [row["embedding_dense"] for row in rows],
                self.vector_batch_size,
            )
            scored_claims.extend((str(row["id"]), score) for row, score in zip(rows, scores))
        scored_claims.sort(key=lambda item: (-item[1], item[0]))

        return materialize_candidates(self, scored_claims, limit, reference, known_as_of, selected_intent)

    def sync_vector(self, claim_id: str) -> None:
        """在调用方事务内同步 Claim 当前 embedding/namespace。"""
        if self.vector_backend is not None:
            self.vector_backend.update(claim_id)

    def delete_vector(self, claim_id: str) -> None:
        """在调用方事务内移除 Claim 的派生向量。"""
        if self.vector_backend is not None:
            self.vector_backend.delete(claim_id)

    def search(
        self,
        query_blob: bytes,
        limit: int,
        reference_time: str,
        intent: RecallIntent,
        known_as_of: str | None,
        namespace: str,
    ) -> list[ClaimRow]:
        """以统一后端协议委托 SQLite 向量扫描。"""
        return cast(
            list[ClaimRow],
            self.search_claims_vector(
                query_blob,
                limit,
                reference_time,
                intent,
                known_as_of,
                namespace,
            ),
        )

    def record_access(self, claim_ids: list[str], accessed_at: str, *, commit: bool = True) -> int:
        """批量累计召回访问次数并记录最近访问时间。"""
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
            if commit:
                self.connection.commit()
            return total
        except Exception:
            if commit:
                self.connection.rollback()
            raise

    def helpful_rates(self, claim_ids: list[str], min_samples: int) -> dict[str, float]:
        """批量返回平滑 usefulness；无样本保持 0.5 prior。"""
        unique_ids = list(dict.fromkeys(claim_ids))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.connection.execute(
            "SELECT c.id AS memory_id,COALESCE(u.helpful_count,0) AS helpful_count,"
            "COALESCE(u.unhelpful_count,0) AS unhelpful_count,COALESCE(u.usefulness_score,0.5) AS usefulness_score "
            "FROM claims c LEFT JOIN memory_usefulness u ON u.memory_type='claim' AND u.memory_id=c.id "
            f"WHERE c.id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        return {
            row["memory_id"]: (
                float(row["usefulness_score"])
                if row["helpful_count"] + row["unhelpful_count"] >= min_samples
                else (row["helpful_count"] + 2) / (row["helpful_count"] + row["unhelpful_count"] + 4)
            )
            for row in rows
        }

    def insert_conflict_case(self, conflict_case: dict[str, Any], commit: bool = True) -> bool:
        """写入幂等冲突审核记录。"""
        return insert_row(self.connection, "conflict_cases", conflict_case, commit)

    def ensure_group_conflict_case(
        self,
        members: Sequence[dict[str, Any]],
        *,
        created_at: str,
        decision: str,
        rationale: str,
        commit: bool = True,
    ) -> dict[str, Any]:
        """创建或复用一个互斥组工单，并折叠挂接全部 canonical candidates。"""

        unique_members = {str(member["id"]): member for member in members}
        if not unique_members:
            raise ConflictError("conflict group must contain at least one member")
        namespaces = {str(member.get("namespace_key") or "") for member in unique_members.values()}
        conflict_keys = {str(member.get("conflict_key") or "") for member in unique_members.values()}
        slots = {str(member.get("canonical_slot") or "") for member in unique_members.values()}
        if (
            len(namespaces) != 1
            or len(conflict_keys) != 1
            or len(slots) != 1
            or not all(is_mutually_exclusive_attribute(slot) for slot in slots)
        ):
            raise ConflictError("group conflict cases require one mutually-exclusive namespace, key, and slot")
        namespace = next(iter(namespaces))
        conflict_key = next(iter(conflict_keys))
        group_key = compute_conflict_group_key(namespace, conflict_key)
        open_case = self.connection.execute(
            "SELECT * FROM conflict_cases WHERE namespace_key=? AND group_key=? "
            "AND status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL",
            (namespace, group_key),
        ).fetchone()
        outcome = "attached"
        if open_case is None:
            candidate_members: dict[str, list[dict[str, Any]]] = {}
            for member in unique_members.values():
                candidate_key = self._conflict_candidate_key(member)
                candidate_members.setdefault(candidate_key, []).append(member)
            if len(candidate_members) < 2:
                raise ConflictError("a new group conflict case requires at least two distinct candidates")
            representative_ids = [
                str(sorted(group, key=lambda item: (str(item.get("recorded_from") or ""), str(item["id"])))[0]["id"])
                for _, group in sorted(candidate_members.items())
            ]
            generation = int(
                self.connection.execute(
                    "SELECT COALESCE(max(generation),0)+1 FROM conflict_cases " "WHERE namespace_key=? AND group_key=?",
                    (namespace, group_key),
                ).fetchone()[0]
            )
            legacy_case = self.connection.execute(
                "SELECT cases.* FROM conflict_cases AS cases "
                "JOIN claims AS left_claim ON left_claim.id=cases.left_claim_id "
                "JOIN claims AS right_claim ON right_claim.id=cases.right_claim_id "
                "WHERE cases.group_key IS NULL "
                "AND cases.status IN ('pending','auto_resolved','manual_required') "
                "AND cases.resolved_at IS NULL "
                "AND left_claim.namespace_key=? AND right_claim.namespace_key=? "
                "AND left_claim.conflict_key=? AND right_claim.conflict_key=? "
                "ORDER BY cases.created_at,cases.id LIMIT 1",
                (namespace, namespace, conflict_key, conflict_key),
            ).fetchone()
            if legacy_case is not None:
                self.connection.execute(
                    "UPDATE conflict_cases SET namespace_key=?,group_key=?,generation=? "
                    "WHERE id=? AND group_key IS NULL",
                    (namespace, group_key, generation, legacy_case["id"]),
                )
                open_case = self.connection.execute(
                    "SELECT * FROM conflict_cases WHERE id=?",
                    (legacy_case["id"],),
                ).fetchone()
                outcome = "adopted"
            else:
                case_id = uuid.uuid4().hex
                try:
                    self.connection.execute(
                        "INSERT INTO conflict_cases("
                        "id,pair_key,left_claim_id,right_claim_id,status,decision,rationale,created_at,"
                        "namespace_key,group_key,generation"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            case_id,
                            compute_conflict_group_case_key(namespace, group_key, generation),
                            representative_ids[0],
                            representative_ids[1],
                            "manual_required",
                            decision,
                            rationale,
                            created_at,
                            namespace,
                            group_key,
                            generation,
                        ),
                    )
                    outcome = "created"
                except sqlite3.IntegrityError as error:
                    if "conflict_cases.namespace_key, conflict_cases.group_key" not in str(error):
                        raise
                    open_case = self.connection.execute(
                        "SELECT * FROM conflict_cases WHERE namespace_key=? AND group_key=? "
                        "AND status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL",
                        (namespace, group_key),
                    ).fetchone()
                    if open_case is None:
                        raise
                else:
                    open_case = self.connection.execute(
                        "SELECT * FROM conflict_cases WHERE id=?", (case_id,)
                    ).fetchone()
        if open_case is None:  # pragma: no cover - guarded by insert/reselect branches
            raise RuntimeError(f"open conflict group disappeared: {namespace}:{group_key}")
        case_id = str(open_case["id"])
        attached_members = 0
        for member in unique_members.values():
            candidate_key = self._conflict_candidate_key(member)
            candidate = self.connection.execute(
                "SELECT 1 FROM conflict_case_candidates WHERE case_id=? AND candidate_key=?",
                (case_id, candidate_key),
            ).fetchone()
            if candidate is None:
                seen_at = str(member.get("recorded_from") or created_at)
                self.connection.execute(
                    "INSERT INTO conflict_case_candidates("
                    "case_id,candidate_key,canonical_value_json,representative_claim_id,"
                    "support_count,first_seen_at,last_seen_at"
                    ") VALUES (?,?,?,?,1,?,?)",
                    (case_id, candidate_key, candidate_key, member["id"], seen_at, seen_at),
                )
            existing_member = self.connection.execute(
                "SELECT 1 FROM conflict_candidate_members WHERE case_id=? AND claim_id=?",
                (case_id, member["id"]),
            ).fetchone()
            if existing_member is not None:
                continue
            attached_at = str(member.get("recorded_from") or created_at)
            self.connection.execute(
                "INSERT INTO conflict_candidate_members(case_id,candidate_key,claim_id,attached_at) "
                "VALUES (?,?,?,?)",
                (case_id, candidate_key, member["id"], attached_at),
            )
            self.connection.execute(
                "UPDATE conflict_case_candidates SET support_count=("
                "SELECT count(*) FROM conflict_candidate_members "
                "WHERE case_id=? AND candidate_key=?"
                "),last_seen_at=max(last_seen_at,?) WHERE case_id=? AND candidate_key=?",
                (case_id, candidate_key, attached_at, case_id, candidate_key),
            )
            attached_members += 1
        candidate_count = int(
            self.connection.execute(
                "SELECT count(*) FROM conflict_case_candidates WHERE case_id=?",
                (case_id,),
            ).fetchone()[0]
        )
        overflow = int(candidate_count > self.conflict_auto_resolve_max_candidates)
        self.connection.execute(
            "UPDATE conflict_cases SET status='manual_required',decision=?,rationale=?,overflow=? "
            "WHERE id=? AND (status IS NOT 'manual_required' OR decision IS NOT ? "
            "OR rationale IS NOT ? OR overflow IS NOT ?)",
            (decision, rationale, overflow, case_id, decision, rationale, overflow),
        )
        current = self.connection.execute(
            "SELECT generation,revision FROM conflict_cases WHERE id=?",
            (case_id,),
        ).fetchone()
        if outcome != "created" and attached_members == 0:
            outcome = "unchanged"
        result = {
            "outcome": outcome,
            "case_id": case_id,
            "generation": int(current["generation"]),
            "revision": int(current["revision"]),
            "candidate_count": candidate_count,
            "overflow": bool(overflow),
        }
        if commit:
            self.connection.commit()
        return result

    @staticmethod
    def _conflict_candidate_key(member: dict[str, Any]) -> str:
        value_json = member.get("value_json")
        if isinstance(value_json, str):
            return value_json
        return encode_json(member.get("value"), sort_keys=True)

    def ensure_manual_conflict_case(
        self,
        conflict_case: dict[str, Any],
        commit: bool = True,
    ) -> str:
        """创建或重开人工复核；既有终态裁决保持不可变。"""
        if insert_row(self.connection, "conflict_cases", conflict_case, commit=False):
            outcome = "created"
        else:
            existing = self.connection.execute(
                "SELECT id,status FROM conflict_cases WHERE pair_key=?",
                (conflict_case["pair_key"],),
            ).fetchone()
            if existing is None:
                raise RuntimeError(f"conflict case insert failed for pair {conflict_case['pair_key']}")
            if existing["status"] == "manual_required":
                outcome = "unchanged"
            elif existing["status"] in {"resolved", "rejected"}:
                outcome = "preserved_terminal"
            else:
                self.connection.execute(
                    "UPDATE conflict_cases SET status='manual_required',decision='uncertain',"
                    "rationale=?,confidence=NULL,resolved_at=NULL WHERE id=?",
                    (conflict_case["rationale"], existing["id"]),
                )
                outcome = "reopened"
        if commit:
            self.connection.commit()
        return outcome

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
                "SELECT id,index_text,conflict_key FROM claims "
                f"WHERE conflict_key IN ({placeholders}) AND status='disputed' AND namespace_key=?",
                (*chunk, namespace),
            ).fetchall()
            for row in rows:
                result[row["conflict_key"]].append(
                    {
                        "id": row["id"],
                        "text": row["index_text"] if isinstance(row["index_text"], str) else "",
                    }
                )
        return result

    @staticmethod
    def _decode_claim(claim: dict[str, Any] | None) -> dict[str, Any] | None:
        """在仓储边界为兼容字典附加已解码的 Python 值。"""
        if claim is None:
            return None
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
                (
                    changed_at,
                    recorded_at,
                    envelope,
                    new_claim_id,
                    old_id,
                    old["status"],
                ),
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
        candidates: list[dict[str, Any]] = (
            [dict(item) for item in self.search_claims_fts(query, limit, valid_as_of, intent, known_as_of, namespace)]
            if query is not None
            else self.search_claims_vector(query_blob or b"", limit, valid_as_of, intent, known_as_of, namespace)
        )
        return [item for item in candidates if claim_is_visible(item, valid_as_of, known_as_of, intent)]

    def retract(self, claim_id: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE claims SET status='retracted',embedding_dense=NULL,embedding_sparse=NULL WHERE id=?",
            (claim_id,),
        )
        if cursor.rowcount == 1:
            self.delete_vector(claim_id)
        self.connection.commit()
        return cursor.rowcount == 1

    def search_claims_fts(
        self,
        query: str,
        limit: int | None = None,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[ClaimRow]:
        """使用 FTS5 查询并应用双时间可见性过滤。"""
        limit = self.recall_default_limit if limit is None else limit
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
        statuses = _recall_statuses_sql(selected_intent)
        match_sql = (
            "SELECT c.* FROM claims_fts_v2 f JOIN claims c ON c.rowid=f.rowid "
            f"WHERE claims_fts_v2 MATCH ? AND c.status IN {statuses} "
            "AND c.namespace_key=? "
            f"{_valid_time_sql(selected_intent, 'c')}"
            "ORDER BY bm25(claims_fts_v2) LIMIT ?"
        )

        def execute_match(match_query: str) -> list[sqlite3.Row]:
            return self.connection.execute(
                match_sql,
                (match_query, namespace, *_valid_time_parameters(selected_intent, reference), limit),
            ).fetchall()

        match_query = prepare_fts_query(query, language=self.fts_language)
        if not match_query:
            return []
        try:
            rows = execute_match(match_query)
        except sqlite3.OperationalError as error:
            if not is_fts_syntax_error(error):
                raise
            return []
        return cast(
            list[ClaimRow],
            [
                claim
                for claim in self._decode_rows(rows)
                if claim_is_visible(claim, reference, known_as_of, selected_intent)
            ],
        )

    def search_archived_claims_fts(
        self,
        query: str,
        limit: int,
        as_of: str | None = None,
        known_as_of: str | None = None,
        namespace: str = "default",
    ) -> list[dict[str, Any]]:
        """Bounded cold FTS over archived claims only, with current-time visibility."""

        reference = as_of or datetime.now(timezone.utc).isoformat()
        match_query = prepare_fts_query(query, language=self.fts_language)
        if not match_query:
            return []
        try:
            rows = self.connection.execute(
                "SELECT c.* FROM claims_fts_v2 f JOIN claims c ON c.rowid=f.rowid "
                "WHERE claims_fts_v2 MATCH ? AND c.status='archived' AND c.namespace_key=? "
                "ORDER BY bm25(claims_fts_v2),c.id LIMIT ?",
                (match_query, namespace, limit),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if not is_fts_syntax_error(error):
                raise
            return []
        visible: list[dict[str, Any]] = []
        for claim in self._decode_rows(rows):
            projected = {**claim, "status": "active"}
            if claim_is_visible(projected, reference, known_as_of, RecallIntent.CURRENT_STATE):
                visible.append(claim)
        return visible

    def search_claims_tags(
        self,
        query_tags: list[str],
        namespace: str = "default",
        limit: int | None = None,
        as_of: str | None = None,
        intent: RecallIntent | str | None = None,
        known_as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """按规范化标签执行 OR 查询，并返回时间可见的 claim。"""
        if not query_tags:
            return []
        limit = self.recall_default_limit if limit is None else limit
        reference = as_of or datetime.now(timezone.utc).isoformat()
        selected_intent = RecallIntent(intent or RecallIntent.CURRENT_STATE)
        statuses = _recall_statuses_sql(selected_intent)
        match_query = " OR ".join(f'"{tag.replace(chr(34), chr(34) * 2)}"' for tag in dict.fromkeys(query_tags))
        try:
            rows = self.connection.execute(
                "SELECT c.* FROM claims_tags_fts_v2 f JOIN claims c ON c.rowid=f.rowid "
                f"WHERE claims_tags_fts_v2 MATCH ? AND c.status IN {statuses} "
                "AND c.namespace_key=? "
                f"{_valid_time_sql(selected_intent, 'c')}"
                "ORDER BY bm25(claims_tags_fts_v2) LIMIT ?",
                (match_query, namespace, *_valid_time_parameters(selected_intent, reference), limit),
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
