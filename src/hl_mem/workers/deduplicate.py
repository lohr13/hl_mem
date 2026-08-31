"""跨主体语义去重后台任务。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from hl_mem.domain.claims.dedup import (
    DEDUP_EMBEDDING_TEXT_VERSION,
    DEDUP_POLICY_VERSION,
    DETERMINISTIC_NEAR_COPY_REASON,
    Deduplicator,
    compute_dedup_pair_key,
    dedup_auto_apply_eligible,
    dedup_structural_gate,
    is_safe_near_duplicate,
)
from hl_mem.domain.governance import DecisionEnvelope, snapshot_fingerprint
from hl_mem.llm.client import LLMClient
from hl_mem.protocols import EmbedderProtocol
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.dedup_candidates import active_entity_proof_key
from hl_mem.storage.governance import GovernanceActionRepository
from hl_mem.workers.dedup_judge import DedupJudge
from hl_mem.workers.scheduling import enqueue_daily_job

EMBEDDING_TEXT_VERSION = DEDUP_EMBEDDING_TEXT_VERSION
POLICY_VERSION = DEDUP_POLICY_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pair_key(left_id: str, right_id: str) -> str:
    return compute_dedup_pair_key(left_id, right_id)


def _record_candidate(connection: sqlite3.Connection, namespace: str, candidate: dict[str, Any]) -> int:
    left = candidate["left"]
    right = candidate["right"]
    cursor = connection.execute(
        "INSERT OR IGNORE INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,pair_source,namespace_key,similarity,"
        "embedding_text_version,policy_version,predicate,candidate_strategy,bucket_key,"
        "entity_proof_id,auto_apply_eligible,created_at"
        ") VALUES (?,?,?,?,'maintenance',?,?,?,?,?,?,?,?,?,?)",
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
            candidate["candidate_strategy"],
            candidate["bucket_key"],
            candidate["entity_proof_id"],
            int(candidate["auto_apply_eligible"]),
            _now(),
        ),
    )
    return cursor.rowcount


def enqueue_daily_deduplication(
    connection: sqlite3.Connection,
    now: str,
    scheduled_minutes: int,
    *,
    enabled: bool = True,
) -> bool:
    """到达计划时间后幂等创建当天的跨主体去重任务。"""
    if not enabled:
        return False
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
    discovered = sum(_record_candidate(connection, namespace, candidate) for candidate in candidates)
    connection.execute(
        "UPDATE dedup_pairs SET policy_version=? "
        "WHERE namespace_key=? AND decision IS NULL AND COALESCE(policy_version,'')<>?",
        (POLICY_VERSION, namespace, POLICY_VERSION),
    )
    connection.commit()

    judge = DedupJudge(llm_client)
    reviewed = equivalent = distinct = uncertain = applied = skipped = 0
    pending_rows = connection.execute(
        "SELECT * FROM dedup_pairs WHERE namespace_key=? AND policy_version=? AND decision IS NULL "
        "ORDER BY similarity DESC,created_at,id LIMIT ?",
        (namespace, POLICY_VERSION, limit),
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

        typed_strategy = pair.get("candidate_strategy") == "slot_cross_subject_v1"
        structural = dedup_structural_gate(left, right, allow_cross_subject=typed_strategy)
        proof_valid = not typed_strategy or active_entity_proof_key(connection, left, right, namespace) == pair.get(
            "entity_proof_id"
        )
        auto_apply_eligible = dedup_auto_apply_eligible(
            left, right, typed_strategy=typed_strategy, proof_valid=proof_valid
        )
        deterministic = Deduplicator._deterministic_check(left, right)
        if typed_strategy and (not structural.safe or not proof_valid):
            deterministic = "distinct"
        elif typed_strategy and Deduplicator._canonical_claim(left) == Deduplicator._canonical_claim(right):
            deterministic = "equivalent"
        elif typed_strategy:
            deterministic = None
        if deterministic is None:
            # 远程调用发生在任何写事务之外。
            decision, confidence, reason = judge.judge(left, right)
            judge_model: str | None = llm_client.model
        else:
            reason = (
                "entity_proof_stale"
                if typed_strategy and not proof_valid
                else (
                    f"structural_gate:{structural.reason}"
                    if typed_strategy and not structural.safe
                    else "deterministic_safety_gate"
                )
            )
            decision, confidence = deterministic, 1.0
            judge_model = None
        reviewed_at = _now()
        cursor = connection.execute(
            "UPDATE dedup_pairs SET decision=?,judge_confidence=?,judge_reason=?,"
            "judge_model=?,reviewed_at=?,auto_apply_eligible=? WHERE id=? AND decision IS NULL",
            (
                decision,
                confidence,
                reason,
                judge_model,
                reviewed_at,
                int(auto_apply_eligible),
                pair["id"],
            ),
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
            "AND policy_version=? AND judge_confidence>=? AND applied_at IS NULL "
            "AND auto_apply_eligible=1 "
            "AND COALESCE(judge_reason,'')<>? "
            "ORDER BY reviewed_at,created_at,id LIMIT ?",
            (
                namespace,
                POLICY_VERSION,
                auto_merge_min_confidence,
                DETERMINISTIC_NEAR_COPY_REASON,
                limit,
            ),
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


def review_pending_near_duplicates(
    connection: sqlite3.Connection,
    *,
    threshold: float = 0.92,
    limit: int = 200,
    reviewed_at: str | None = None,
) -> dict[str, int]:
    """Confirm only conservative near-copy pairs without mutating either claim."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("dedup threshold must be between 0 and 1")
    if limit < 1:
        raise ValueError("dedup review limit must be positive")

    pending_rows: list[sqlite3.Row] = []
    equivalent = 0
    missing = 0
    stamp = reviewed_at or _now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        pending_rows = connection.execute(
            "SELECT id,left_claim_id,right_claim_id,similarity FROM dedup_pairs "
            "WHERE decision IS NULL AND similarity>=? "
            "ORDER BY CASE WHEN reviewed_at IS NULL THEN 0 ELSE 1 END,"
            "reviewed_at,similarity DESC,created_at,id LIMIT ?",
            (threshold, limit),
        ).fetchall()
        repository = ClaimRepository(connection)
        claim_ids = {
            str(claim_id) for row in pending_rows for claim_id in (row["left_claim_id"], row["right_claim_id"])
        }
        claims = repository.batch_get_claims(sorted(claim_ids))
        for row in pending_rows:
            left = claims.get(row["left_claim_id"])
            right = claims.get(row["right_claim_id"])
            if left is None or right is None:
                missing += 1
                connection.execute(
                    "UPDATE dedup_pairs SET reviewed_at=? WHERE id=? AND decision IS NULL",
                    (stamp, row["id"]),
                )
                continue
            if is_safe_near_duplicate(
                left,
                right,
                similarity=float(row["similarity"]),
                semantic_threshold=threshold,
                allow_subject_mismatch=True,
            ):
                cursor = connection.execute(
                    "UPDATE dedup_pairs SET decision='equivalent',judge_confidence=similarity,"
                    "judge_reason=?,judge_model=NULL,reviewed_at=? "
                    "WHERE id=? AND decision IS NULL",
                    (DETERMINISTIC_NEAR_COPY_REASON, stamp, row["id"]),
                )
                equivalent += cursor.rowcount
            else:
                connection.execute(
                    "UPDATE dedup_pairs SET reviewed_at=? WHERE id=? AND decision IS NULL",
                    (stamp, row["id"]),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "scanned": len(pending_rows),
        "equivalent": equivalent,
        "deferred": len(pending_rows) - missing - equivalent,
        "missing": missing,
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
            "SELECT decision,judge_confidence,judge_reason,policy_version,applied_at,"
            "candidate_strategy,entity_proof_id,auto_apply_eligible FROM dedup_pairs WHERE id=?",
            (pair_id,),
        ).fetchone()
        repository = ClaimRepository(connection)
        current = repository.batch_get_claims([left["id"], right["id"]])
        current_left = current.get(left["id"])
        current_right = current.get(right["id"])
        if pair is None or current_left is None or current_right is None:
            connection.rollback()
            return False
        typed_strategy = pair["candidate_strategy"] == "slot_cross_subject_v1"
        structural = dedup_structural_gate(current_left, current_right, allow_cross_subject=typed_strategy)
        proof_valid = (
            not typed_strategy
            or active_entity_proof_key(
                connection,
                current_left,
                current_right,
                str(current_left.get("namespace_key") or "default"),
            )
            == pair["entity_proof_id"]
        )
        if not proof_valid:
            connection.execute(
                "UPDATE dedup_pairs SET auto_apply_eligible=0,judge_reason='entity_proof_stale' WHERE id=?",
                (pair_id,),
            )
            connection.commit()
            return False
        stale = (
            pair["decision"] != "equivalent"
            or float(pair["judge_confidence"] or 0.0) < min_confidence
            or pair["judge_reason"] == DETERMINISTIC_NEAR_COPY_REASON
            or pair["policy_version"] != POLICY_VERSION
            or pair["applied_at"] is not None
            or not bool(pair["auto_apply_eligible"])
            or current_left["status"] != "active"
            or current_right["status"] != "active"
            or current_left["recorded_from"] != left.get("recorded_from")
            or current_right["recorded_from"] != right.get("recorded_from")
            or not structural.safe
        )
        if stale:
            connection.rollback()
            return False
        survivor, losing = _select_survivor(connection, current_left, current_right)
        evidence_before = _claim_evidence_ids(connection, survivor["id"])
        before = _dedup_action_state(connection, (survivor["id"], losing["id"]), pair_id, ())
        connection.execute(
            "INSERT OR IGNORE INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation,weight"
            ") SELECT lower(hex(randomblob(16))),derived_type,?,evidence_type,evidence_id,relation,weight "
            "FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
            (survivor["id"], losing["id"]),
        )
        result = repository.supersede_with_inline(
            losing["id"],
            survivor["id"],
            survivor["value"],
            survivor.get("valid_from") or survivor["recorded_from"],
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
        created_links = sorted(_claim_evidence_ids(connection, survivor["id"]) - evidence_before)
        fingerprint = snapshot_fingerprint(
            {
                "pair": {
                    "id": pair_id,
                    "decision": pair["decision"],
                    "confidence": pair["judge_confidence"],
                    "strategy": pair["candidate_strategy"],
                },
                "claims": [_dedup_input(current_left), _dedup_input(current_right)],
            }
        )
        GovernanceActionRepository(connection).record(
            DecisionEnvelope(
                domain="dedup",
                subject_ref=pair_id,
                input_fingerprint=fingerprint,
                policy_version=POLICY_VERSION,
                tier="high_confidence",
                decision="equivalent",
                confidence=float(pair["judge_confidence"]),
                resolution_rule="typed_structural_gate",
                resolver_model=None,
                evidence_ids=tuple(created_links),
            ),
            before=before,
            after=_dedup_action_state(
                connection,
                (survivor["id"], losing["id"]),
                pair_id,
                created_links,
            ),
            status="applied",
            created_at=applied_at,
            applied_at=applied_at,
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


_AUTHORITY_RANK = {"low": 0, "medium": 1, "high": 2}
_ROLLBACK_FIELDS = ("status", "valid_to", "recorded_to", "value_json", "superseded_by_id")


def _claim_evidence_ids(connection: sqlite3.Connection, claim_id: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT id FROM evidence_links WHERE derived_type='claim' AND derived_id=?",
            (claim_id,),
        )
    }


def _coordinate_completeness(claim: dict[str, Any]) -> int:
    fields = (
        "subject_canonical_entity_id",
        "canonical_target_entity_id",
        "canonical_slot",
        "canonical_attribute",
        "assertion_kind",
    )
    return sum(claim.get(field) not in (None, "", "unknown") for field in fields) + len(claim.get("qualifiers") or {})


def _select_survivor(
    connection: sqlite3.Connection,
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    def rank(claim: dict[str, Any]) -> tuple[int, int, int, str, str]:
        return (
            _AUTHORITY_RANK.get(str(claim.get("source_authority") or "medium"), 1),
            len(_claim_evidence_ids(connection, str(claim["id"]))),
            _coordinate_completeness(claim),
            str(claim.get("recorded_from") or ""),
            str(claim["id"]),
        )

    survivor = max((left, right), key=rank)
    return survivor, right if survivor is left else left


def _dedup_input(claim: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "namespace_key",
        "subject_entity_id",
        "subject_canonical_entity_id",
        "canonical_target_entity_id",
        "predicate",
        "value",
        "qualifiers",
        "canonical_slot",
        "canonical_attribute",
        "assertion_kind",
        "valid_from",
        "valid_to",
        "recorded_from",
        "recorded_to",
        "status",
        "source_authority",
    )
    return {field: claim.get(field) for field in fields}


def _dedup_action_state(
    connection: sqlite3.Connection,
    claim_ids: tuple[str, str],
    pair_id: str,
    created_link_ids: list[str] | tuple[()],
) -> dict[str, Any]:
    claims = {}
    for claim_id in sorted(claim_ids):
        row = connection.execute(
            f"SELECT {','.join(_ROLLBACK_FIELDS)} FROM claims WHERE id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise KeyError(claim_id)
        claims[claim_id] = {field: row[field] for field in _ROLLBACK_FIELDS}
    pair = connection.execute("SELECT applied_at FROM dedup_pairs WHERE id=?", (pair_id,)).fetchone()
    if pair is None:
        raise KeyError(pair_id)
    return {
        "claims": claims,
        "created_evidence_links": {
            link_id: bool(connection.execute("SELECT 1 FROM evidence_links WHERE id=?", (link_id,)).fetchone())
            for link_id in sorted(created_link_ids)
        },
        "pair": {"id": pair_id, "applied_at": pair["applied_at"]},
    }


def rollback_dedup_action(
    connection: sqlite3.Connection,
    action_id: str,
    *,
    rolled_back_at: str,
    reason: str,
) -> None:
    """Restore one dedup loser only while the exact action after-state remains current."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM governance_actions WHERE id=?", (action_id,)).fetchone()
        if row is None or row["domain"] != "dedup":
            raise KeyError(action_id)
        after = json.loads(str(row["after_json"]))
        claim_ids = tuple(sorted(str(claim_id) for claim_id in after["claims"]))
        if len(claim_ids) != 2:
            raise ValueError("dedup action must reference two claims")
        link_ids = list(after["created_evidence_links"])
        current = _dedup_action_state(connection, (claim_ids[0], claim_ids[1]), row["subject_ref"], link_ids)
        before = GovernanceActionRepository(connection).mark_rolled_back(
            action_id,
            current=current,
            reason=reason,
            rolled_back_at=rolled_back_at,
        )
        for claim_id, state in before["claims"].items():
            connection.execute(
                "UPDATE claims SET status=?,valid_to=?,recorded_to=?,value_json=?,superseded_by_id=? WHERE id=?",
                (*(state[field] for field in _ROLLBACK_FIELDS), claim_id),
            )
        for link_id in link_ids:
            connection.execute("DELETE FROM evidence_links WHERE id=?", (link_id,))
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
