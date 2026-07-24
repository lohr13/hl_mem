"""跨主体语义去重后台任务。"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from hl_mem.domain.claims.dedup import DedupJudge
from hl_mem.llm.client import LLMClient
from hl_mem.protocols import EmbedderProtocol
from hl_mem.storage.claims import ClaimRepository

EMBEDDING_TEXT_VERSION = "v1: predicate+value"
POLICY_VERSION = "v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pair_key(left_id: str, right_id: str) -> str:
    ordered = "\x1f".join(sorted((left_id, right_id)))
    return hashlib.sha256(ordered.encode("utf-8")).hexdigest()


def enqueue_daily_deduplication(connection: sqlite3.Connection, now: str, cron: str) -> bool:
    """到达计划时间后幂等创建当天的跨主体去重任务。"""
    try:
        hour_text, minute_text = cron.split(":", 1)
        scheduled_minutes = int(hour_text) * 60 + int(minute_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("HL_MEM_DEDUP_CRON must use HH:MM format") from error
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not 0 <= scheduled_minutes < 24 * 60:
        raise ValueError("HL_MEM_DEDUP_CRON must use HH:MM format")
    if current.hour * 60 + current.minute < scheduled_minutes:
        return False
    from hl_mem.storage.jobs import JobRepository

    created = JobRepository(connection).insert_job(
        {
            "id": uuid.uuid4().hex,
            "job_type": "deduplicate_claims",
            "payload_json": "{}",
            "idempotency_key": f"deduplicate:{current.date().isoformat()}",
            "created_at": now,
            "updated_at": now,
        }
    )
    connection.commit()
    return created


def deduplicate_claims(
    connection: sqlite3.Connection,
    llm_client: LLMClient,
    embedder: EmbedderProtocol,
    *,
    namespace: str = "default",
    threshold: float = 0.92,
    audit_only: bool = True,
    limit: int = 200,
) -> dict[str, int]:
    """发现、审计并可选合并跨主体重复 Claim。"""
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
    for pending_row in pending_rows:
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
            "AND applied_at IS NULL ORDER BY reviewed_at,created_at,id LIMIT ?",
            (namespace, limit),
        ).fetchall()
        for equivalent_row in equivalent_rows:
            pair = dict(equivalent_row)
            left = repository.get_claim(pair["left_claim_id"])
            right = repository.get_claim(pair["right_claim_id"])
            if not left or not right:
                skipped += 1
                continue
            if _apply_equivalent_pair(connection, pair["id"], left, right, _now()):
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
) -> bool:
    """在短写事务中把右侧 Claim 安全替换为左侧 Claim。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        statuses = connection.execute(
            "SELECT id,status FROM claims WHERE id IN (?,?)",
            (left["id"], right["id"]),
        ).fetchall()
        if len(statuses) != 2 or any(row["status"] != "active" for row in statuses):
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
            left.get("valid_from") or left["recorded_from"],
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
