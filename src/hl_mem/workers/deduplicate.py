"""跨主体语义去重后台任务。"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from hl_mem.domain.claims.dedup import DedupJudge
from hl_mem.llm.client import LLMClient
from hl_mem.protocols import EmbedderProtocol
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.scheduling import enqueue_daily_job

EMBEDDING_TEXT_VERSION = "v1: predicate+value"
POLICY_VERSION = "v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pair_key(left_id: str, right_id: str) -> str:
    ordered = "\x1f".join(sorted((left_id, right_id)))
    return hashlib.sha256(ordered.encode("utf-8")).hexdigest()


def enqueue_daily_deduplication(connection: sqlite3.Connection, now: str, scheduled_minutes: int) -> bool:
    """到达计划时间后幂等创建当天的跨主体去重任务。"""
    return (
        enqueue_daily_job(
            connection,
            now,
            {
                "scheduled_minutes": scheduled_minutes,
                "idempotency_prefix": "deduplicate",
            },
            "deduplicate_claims",
            {},
            "scheduled_minutes",
        )
        is not None
    )


def deduplicate_claims(
    connection: sqlite3.Connection,
    llm_client: LLMClient,
    embedder: EmbedderProtocol,
    *,
    namespace: str = "default",
    threshold: float = 0.92,
    audit_only: bool = True,
    auto_merge_min_confidence: float = 0.98,
    limit: int = 200,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, int]:
    """发现、审计并可选合并跨主体重复 Claim。"""
    if not threshold <= auto_merge_min_confidence <= 1.0:
        raise ValueError("auto merge confidence must be between threshold and 1")
    repository = ClaimRepository(connection)
    candidates = repository.find_cross_subject_dedup_candidates(
        namespace,
        embedder,
        threshold=threshold,
        limit=limit,
    )
    discovered = 0
    for candidate in candidates:
        left = candidate["left"]
        right = candidate["right"]
        cursor = connection.execute(
            "INSERT OR IGNORE INTO dedup_pairs("
            "id,pair_key,left_claim_id,right_claim_id,namespace_key,similarity,"
            "embedding_text_version,policy_version,predicate,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                _pair_key(left["id"], right["id"]),
                left["id"],
                right["id"],
                namespace,
                candidate["similarity"],
                EMBEDDING_TEXT_VERSION,
                POLICY_VERSION,
                left.get("predicate"),
                _now(),
            ),
        )
        discovered += cursor.rowcount
    connection.commit()

    judge = DedupJudge(llm_client)
    reviewed = equivalent = distinct = uncertain = applied = skipped = 0
    pending_rows = connection.execute(
        "SELECT * FROM dedup_pairs WHERE namespace_key=? AND decision IS NULL "
        "ORDER BY similarity DESC,created_at,id LIMIT ?",
        (namespace, limit),
    ).fetchall()
    pending_total = len(pending_rows)
    for processed, pending_row in enumerate(pending_rows, start=1):
        if progress_callback is not None:
            progress_callback("review", processed, pending_total)
        pair = dict(pending_row)
        left = repository.get_claim(pair["left_claim_id"])
        right = repository.get_claim(pair["right_claim_id"])
        if not left or not right:
            skipped += 1
            continue

        # 远程调用发生在任何写事务之外。
        decision, confidence, reason = judge.judge(left, right)
        reviewed_at = _now()
        cursor = connection.execute(
            "UPDATE dedup_pairs SET decision=?,judge_confidence=?,judge_reason=?,"
            "judge_model=?,reviewed_at=? WHERE id=? AND decision IS NULL",
            (decision, confidence, reason, llm_client.model, reviewed_at, pair["id"]),
        )
        connection.commit()
        if cursor.rowcount != 1:
            skipped += 1
            continue
        reviewed += 1
        if decision == "equivalent":
            equivalent += 1
        elif decision == "distinct":
            distinct += 1
        else:
            uncertain += 1
    if not audit_only:
        equivalent_rows = connection.execute(
            "SELECT * FROM dedup_pairs WHERE namespace_key=? AND decision='equivalent' "
            "AND judge_confidence>=? AND applied_at IS NULL ORDER BY reviewed_at,created_at,id LIMIT ?",
            (auto_merge_min_confidence, limit),
        ).fetchall()
        equivalent_total = len(equivalent_rows)
        for processed, equivalent_row in enumerate(equivalent_rows, start=1):
            if progress_callback is not None:
                progress_callback("apply", processed, equivalent_total)
            pair = dict(equivalent_row)
            left = repository.get_claim(pair["left_claim_id"])
            right = repository.get_claim(pair["right_claim_id"])
            if not left or not right:
                skipped += 1
                continue
            if _apply_equivalent_pair(
                connection,
                pair["id"],
                left,
                right,
                _now(),
                auto_merge_min_confidence,
            ):
                applied += 1
            else:
                skipped += 1

    return {
        "discovered": discovered,
        "reviewed": reviewed,
        "equivalent": equivalent,
        "distinct": distinct,
        "uncertain": uncertain,
        "applied": applied,
        "skipped": skipped,
    }


def _apply_equivalent_pair(
    connection: sqlite3.Connection,
    pair_id: str,
    left: dict[str, Any],
    right: dict[str, Any],
    applied_at: str,
    min_confidence: float = 0.98,
) -> bool:
    """在短写事务中把右侧 Claim 安全替换为左侧 Claim。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        pair = connection.execute(
            "SELECT decision,judge_confidence,applied_at FROM dedup_pairs WHERE id=?",
            (pair_id,),
        ).fetchone()
        current_rows = connection.execute(
            "SELECT * FROM claims WHERE id IN (?,?)",
            (left["id"], right["id"]),
        ).fetchall()
        current = {row["id"]: dict(row) for row in current_rows}
        current_left = current.get(left["id"])
        current_right = current.get(right["id"])
        stale = (
            pair is None
            or pair["decision"] != "equivalent"
            or float(pair["judge_confidence"] or 0.0) < min_confidence
            or pair["applied_at"] is not None
            or current_left is None
            or current_right is None
            or current_left["status"] != "active"
            or current_right["status"] != "active"
            or current_left["recorded_from"] != left.get("recorded_from")
            or current_right["recorded_from"] != right.get("recorded_from")
            or current_left["predicate"] != current_right["predicate"]
            or current_left["canonical_slot"] is not None
            or current_right["canonical_slot"] is not None
            or current_left["canonical_attribute"] in {"memory.explicit", "identity.name"}
            or current_right["canonical_attribute"] in {"memory.explicit", "identity.name"}
        )
        if stale:
            connection.rollback()
            return False
        connection.execute(
            "INSERT OR IGNORE INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation,weight"
            ") SELECT lower(hex(randomblob(16))),derived_type,?,evidence_type,evidence_id,relation,weight "
            "FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
            (left["id"], right["id"]),
        )
        result = ClaimRepository(connection).supersede_with_inline(
            right["id"],
            left["id"],
            left["value"],
            current_left.get("valid_from") or current_left["recorded_from"],
            applied_at,
            commit=False,
        )
        if not result.applied:
            connection.rollback()
            return False
        cursor = connection.execute(
            "UPDATE dedup_pairs SET applied_at=? WHERE id=? AND applied_at IS NULL",
            (applied_at, pair_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
