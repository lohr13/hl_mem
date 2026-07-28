"""安全回填 claim 索引文本及其 dense embedding。"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from hl_mem.domain.claims.claim import IndexTextMode, build_index_text
from hl_mem.protocols import EmbedderProtocol


@dataclass
class IndexBackfillSummary:
    """记录一次索引回填的进度、结果与 provider 消耗。"""

    version: str
    mode: IndexTextMode
    dry_run: bool
    provider_model: str
    backfilled: int = 0
    would_backfill: int = 0
    skipped: int = 0
    failed: int = 0
    provider_items: int = 0
    provider_requests: int = 0
    estimated_provider_items: int = 0
    estimated_provider_requests: int = 0
    next_cursor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回适合 CLI 输出的审计摘要。"""
        return asdict(self)


def _text_hash(text: str | None) -> str:
    """计算稳定的 UTF-8 文本摘要。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _claim_for_index(row: sqlite3.Row) -> dict[str, Any]:
    """将数据库原始字段解码为索引文本构造器所需结构。"""
    return {
        "subject_entity_id": row["subject_entity_id"],
        "predicate": row["predicate"],
        "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
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
    """分批回填 active claim，并以源字段和旧索引文本执行 compare-and-set。"""
    if batch_size < 1 or max_attempts < 1:
        raise ValueError("batch_size and max_attempts must be positive")
    summary = IndexBackfillSummary(
        version=version,
        mode=mode,
        dry_run=dry_run,
        provider_model=embedder.model,
        next_cursor=cursor,
    )
    current_cursor = cursor or ""
    max_embed_batch = max(1, int(getattr(embedder, "MAX_BATCH_SIZE", batch_size)))

    while True:
        rows = connection.execute(
            "SELECT id,subject_entity_id,predicate,value_json,canonical_slot,topic_tags_json,index_text "
            "FROM claims WHERE status='active' AND id>? ORDER BY id LIMIT ?",
            (current_cursor, batch_size),
        ).fetchall()
        if not rows:
            break
        current_cursor = str(rows[-1]["id"])
        summary.next_cursor = current_cursor
        pending: list[tuple[sqlite3.Row, str]] = []
        for row in rows:
            target = build_index_text(_claim_for_index(row), mode=mode)
            if _text_hash(row["index_text"]) == _text_hash(target):
                summary.skipped += 1
            else:
                pending.append((row, target))
        if not pending:
            continue
        estimated_requests = math.ceil(len(pending) / max_embed_batch)
        summary.estimated_provider_items += len(pending)
        summary.estimated_provider_requests += estimated_requests
        if dry_run:
            summary.would_backfill += len(pending)
            continue

        embeddings: list[bytes] | None = None
        for attempt in range(1, max_attempts + 1):
            summary.provider_items += len(pending)
            summary.provider_requests += estimated_requests
            try:
                embeddings = embedder.embed_batch([target for _, target in pending])
                break
            except Exception:
                if attempt == max_attempts:
                    summary.failed += len(pending)
        if embeddings is None:
            continue
        if len(embeddings) != len(pending):
            summary.failed += len(pending)
            continue

        try:
            connection.execute("BEGIN IMMEDIATE")
            applied = 0
            skipped = 0
            for (row, target), embedding in zip(pending, embeddings, strict=True):
                result = connection.execute(
                    "UPDATE claims SET index_text=?,embedding_dense=?,embedding_model=?,embedding_dim=? "
                    "WHERE id=? AND status='active' "
                    "AND subject_entity_id IS ? AND predicate IS ? AND value_json IS ? "
                    "AND canonical_slot IS ? AND topic_tags_json IS ? AND index_text IS ?",
                    (
                        target,
                        embedding,
                        embedder.model,
                        embedder.dim,
                        row["id"],
                        row["subject_entity_id"],
                        row["predicate"],
                        row["value_json"],
                        row["canonical_slot"],
                        row["topic_tags_json"],
                        row["index_text"],
                    ),
                )
                if result.rowcount == 1:
                    applied += 1
                else:
                    skipped += 1
            connection.commit()
            summary.backfilled += applied
            summary.skipped += skipped
        except Exception:
            connection.rollback()
            summary.failed += len(pending)
    return summary
