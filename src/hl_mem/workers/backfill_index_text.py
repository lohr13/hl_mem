"""安全回填 claim 索引文本及其 dense embedding。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any

from hl_mem.domain.claims.claim import IndexTextMode, build_index_text
from hl_mem.llm.client import classify_provider_error
from hl_mem.protocols import EmbedderProtocol
from hl_mem.recall.lexicalizer import prepare_fts_document
from hl_mem.workers.index_integrity import check_index_integrity


@dataclass
class IndexBackfillSummary:
    """记录一次索引回填的进度、结果与 provider 消耗。"""

    version: str
    mode: IndexTextMode
    dry_run: bool
    provider_model: str
    provider_dim: int = 0
    scanned: int = 0
    backfilled: int = 0
    would_backfill: int = 0
    skipped: int = 0
    failed: int = 0
    provider_items: int = 0
    provider_requests: int = 0
    estimated_provider_items: int = 0
    estimated_provider_requests: int = 0
    model_version_reembedded: int = 0
    last_error_class: str | None = None
    next_cursor: str | None = None
    text_hash: str = ""
    coverage_complete: bool = True
    integrity_ok: bool | None = None
    integrity: dict[str, Any] | None = None

    @property
    def would_update(self) -> int:
        """返回 dry-run 将更新的行数，保留旧字段并提供明确别名。"""
        return self.would_backfill

    @property
    def skip(self) -> int:
        """返回无需更新的行数，保留旧字段并提供明确别名。"""
        return self.skipped

    @property
    def cursor(self) -> str | None:
        """返回最后扫描到的 Claim 游标。"""
        return self.next_cursor

    @property
    def model(self) -> str:
        """返回目标 embedding model。"""
        return self.provider_model

    @property
    def dim(self) -> int:
        """返回目标 embedding 维度。"""
        return self.provider_dim

    def to_dict(self) -> dict[str, Any]:
        """返回适合 CLI 输出的审计摘要。"""
        result = asdict(self)
        result.update(
            {
                "would_update": self.would_update,
                "skip": self.skip,
                "cursor": self.cursor,
                "model": self.model,
                "dim": self.dim,
            }
        )
        return result


def _text_hash(text: str | None) -> str:
    """计算稳定的 UTF-8 文本摘要。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _embedding_blob_is_valid(embedding: object, dim: int) -> bool:
    """验证 SQLite 值是目标维度的 float32 BLOB。"""
    return isinstance(embedding, (bytes, bytearray, memoryview)) and len(embedding) == 4 * dim


def _provider_error_class(error: Exception) -> str:
    """沿异常链提取稳定的 provider 错误分类。"""
    current: BaseException | None = error
    while isinstance(current, Exception):
        error_class, _, _ = classify_provider_error(current)
        if error_class in {"http_timeout", "rate_limit", "upstream"}:
            return error_class
        current = current.__cause__ or current.__context__
    return type(error).__name__


def _claim_for_index(row: sqlite3.Row) -> dict[str, Any]:
    """将数据库原始字段解码为索引文本构造器所需结构。"""
    return {
        "subject_entity_id": row["subject_entity_id"],
        "predicate": row["predicate"],
        "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
        "qualifiers": json.loads(row["qualifiers_json"]) if row["qualifiers_json"] is not None else None,
        "canonical_slot": row["canonical_slot"],
        "topic_tags": json.loads(row["topic_tags_json"] or "[]"),
    }


def backfill_index_text(
    connection: sqlite3.Connection,
    embedder: EmbedderProtocol,
    *,
    mode: IndexTextMode,
    version: str,
    batch_size: int,
    max_attempts: int,
    dry_run: bool = False,
    cursor: str | None = None,
) -> IndexBackfillSummary:
    """分批回填可召回 Claim，并以源字段和旧派生字段执行 compare-and-set。"""
    if batch_size < 1 or max_attempts < 1:
        raise ValueError("batch_size and max_attempts must be positive")
    if mode not in {"legacy", "value_only", "natural", "answerable"}:
        raise ValueError(f"unsupported index_text mode: {mode}")
    if embedder.dim < 1:
        raise ValueError("embedder.dim must be positive")
    summary = IndexBackfillSummary(
        version=version,
        mode=mode,
        dry_run=dry_run,
        provider_model=embedder.model,
        provider_dim=embedder.dim,
        next_cursor=cursor,
    )
    current_cursor = cursor or ""
    safe_cursor = cursor
    cursor_blocked = False
    max_embed_batch = max(1, int(getattr(embedder, "MAX_BATCH_SIZE", batch_size)))
    target_text_digest = hashlib.sha256()

    while True:
        rows = connection.execute(
            "SELECT rowid AS claim_rowid,id,status,subject_entity_id,predicate,value_json,qualifiers_json,canonical_slot,"
            "topic_tags_json,index_text,embedding_dense,embedding_model,embedding_dim "
            "FROM claims WHERE status IN ('active','superseded','expired') "
            "AND id>? ORDER BY id LIMIT ?",
            (current_cursor, batch_size),
        ).fetchall()
        if not rows:
            break
        batch_cursor = str(rows[-1]["id"])
        current_cursor = batch_cursor
        failed_before_batch = summary.failed

        def finish_batch() -> None:
            """仅在此前及本批均无失败时推进可安全续跑的游标。"""
            nonlocal cursor_blocked, safe_cursor
            if not cursor_blocked and summary.failed == failed_before_batch:
                safe_cursor = batch_cursor
            else:
                cursor_blocked = True
            summary.next_cursor = safe_cursor

        pending: list[tuple[sqlite3.Row, str]] = []
        for row in rows:
            summary.scanned += 1
            try:
                target = build_index_text(_claim_for_index(row), mode=mode)
            except Exception as error:
                summary.failed += 1
                summary.last_error_class = type(error).__name__
                continue
            target_text_digest.update(
                json.dumps(
                    [str(row["id"]), target],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            target_text_digest.update(b"\n")
            text_changed = _text_hash(row["index_text"]) != _text_hash(target)
            model_changed = row["embedding_model"] != embedder.model or row["embedding_dim"] != embedder.dim
            embedding = row["embedding_dense"]
            embedding_invalid = not _embedding_blob_is_valid(embedding, embedder.dim)
            if not text_changed and not model_changed and not embedding_invalid:
                summary.skipped += 1
            else:
                pending.append((row, target))
                if model_changed:
                    summary.model_version_reembedded += 1
        if not pending:
            finish_batch()
            continue
        estimated_requests = math.ceil(len(pending) / max_embed_batch)
        summary.estimated_provider_items += len(pending)
        summary.estimated_provider_requests += estimated_requests
        if dry_run:
            summary.would_backfill += len(pending)
            finish_batch()
            continue

        embeddings: list[bytes] | None = None
        for attempt in range(1, max_attempts + 1):
            summary.provider_items += len(pending)
            summary.provider_requests += estimated_requests
            try:
                embeddings = embedder.embed_batch([target for _, target in pending])
                break
            except Exception as error:
                error_class = _provider_error_class(error)
                summary.last_error_class = error_class
                recoverable = error_class in {"http_timeout", "rate_limit", "upstream"}
                if attempt == max_attempts or not recoverable:
                    summary.failed += len(pending)
                    break
                time.sleep(attempt * 2)
        if embeddings is None:
            finish_batch()
            continue
        if len(embeddings) != len(pending):
            summary.failed += len(pending)
            summary.last_error_class = "EmbeddingCountMismatch"
            finish_batch()
            continue
        if any(not _embedding_blob_is_valid(embedding, embedder.dim) for embedding in embeddings):
            summary.failed += len(pending)
            summary.last_error_class = "EmbeddingDimensionMismatch"
            finish_batch()
            continue

        try:
            connection.execute("BEGIN IMMEDIATE")
            applied = 0
            failed = 0
            for (row, target), embedding in zip(pending, embeddings, strict=True):
                result = connection.execute(
                    "UPDATE claims SET index_text=?,embedding_dense=?,embedding_model=?,embedding_dim=? "
                    "WHERE id=? AND status=? "
                    "AND subject_entity_id IS ? AND predicate IS ? AND value_json IS ? "
                    "AND qualifiers_json IS ? AND canonical_slot IS ? AND topic_tags_json IS ? "
                    "AND index_text IS ? AND embedding_dense IS ? AND embedding_model IS ? "
                    "AND embedding_dim IS ?",
                    (
                        target,
                        embedding,
                        embedder.model,
                        embedder.dim,
                        row["id"],
                        row["status"],
                        row["subject_entity_id"],
                        row["predicate"],
                        row["value_json"],
                        row["qualifiers_json"],
                        row["canonical_slot"],
                        row["topic_tags_json"],
                        row["index_text"],
                        row["embedding_dense"],
                        row["embedding_model"],
                        row["embedding_dim"],
                    ),
                )
                if result.rowcount == 1:
                    connection.execute(
                        "INSERT OR REPLACE INTO claims_fts_v2(rowid,terms) VALUES(?,?)",
                        (row["claim_rowid"], prepare_fts_document(target)),
                    )
                    applied += 1
                else:
                    failed += 1
            connection.commit()
            summary.backfilled += applied
            summary.failed += failed
            if failed:
                summary.last_error_class = "CompareAndSetMismatch"
        except Exception as error:
            connection.rollback()
            summary.failed += len(pending)
            summary.last_error_class = type(error).__name__
        finish_batch()
    summary.text_hash = target_text_digest.hexdigest()
    completed = summary.skipped + summary.failed
    completed += summary.would_backfill if dry_run else summary.backfilled
    summary.coverage_complete = summary.failed == 0 and completed == summary.scanned
    if not dry_run:
        try:
            integrity = check_index_integrity(
                connection,
                mode=mode,
                expected_model=embedder.model,
                expected_dim=embedder.dim,
            )
            summary.integrity = integrity.to_dict()
            summary.integrity_ok = integrity.ok
        except Exception as error:
            summary.integrity = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }
            summary.integrity_ok = False
        summary.coverage_complete = summary.coverage_complete and bool(summary.integrity_ok)
        if not summary.integrity_ok:
            if summary.failed == 0:
                summary.next_cursor = None
            if summary.last_error_class is None:
                summary.last_error_class = "IndexIntegrityError"
    return summary
