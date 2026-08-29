"""冲突案卷、CAS 应用、有界维护入口及领域法条兼容导出。"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from hl_mem.application.conflict_invariants import assert_global_conflict_postconditions
from hl_mem.application.conflict_snapshot import (
    conflict_docket_fingerprint,
    load_conflict_docket,
)
from hl_mem.application.conflicts import (
    ResolutionService,
    StaleConflictDecision,
)
from hl_mem.domain.governance import (
    CONFLICT_AUTO_POLICY_VERSION,
    AutoDecision,
    DecisionEnvelope,
    decide_l0,
)
from hl_mem.storage.governance import (
    GovernanceActionRepository,
    upgrade_conflict_auto_policy,
)

__all__ = [
    "AutoDecision",
    "StaleConflictDecision",
    "auto_resolve_conflicts",
    "decide_l0",
]


def _mutation_snapshot(connection: sqlite3.Connection, case_id: str, claim_ids: list[str]) -> dict[str, Any]:
    case = connection.execute(
        "SELECT id,status,decision,resolved_at,revision,policy_version,last_tier,resolution_rule,resolver_model "
        "FROM conflict_cases WHERE id=?",
        (case_id,),
    ).fetchone()
    placeholders = ",".join("?" for _ in claim_ids)
    claims = connection.execute(
        "SELECT id,status,valid_to,recorded_to,superseded_by_id FROM claims "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        claim_ids,
    ).fetchall()
    return {"case": dict(case), "claims": [dict(claim) for claim in claims]}


def apply_auto_conflict_decision(
    connection: sqlite3.Connection,
    case_id: str,
    decision: Any,
    *,
    expected_revision: int,
    expected_fingerprint: str,
    policy_version: str,
    now: str,
) -> dict[str, Any]:
    """重读 revision/fingerprint，并在单一短事务中应用 mutation 与 ledger。"""

    connection.execute("BEGIN IMMEDIATE")
    try:
        docket = load_conflict_docket(connection, case_id)
        current_revision = int(docket["case"].get("revision") or 0)
        current_fingerprint = conflict_docket_fingerprint(docket)
        if current_revision != expected_revision or current_fingerprint != expected_fingerprint:
            raise StaleConflictDecision(f"stale conflict decision: {case_id}")
        claim_ids = [str(claim["id"]) for claim in docket["claims"]]
        before = _mutation_snapshot(connection, case_id, claim_ids)
        envelope = DecisionEnvelope(
            domain="conflict",
            subject_ref=case_id,
            input_fingerprint=expected_fingerprint,
            policy_version=policy_version,
            tier=str(decision.tier),
            decision=str(decision.decision),
            confidence=float(decision.confidence),
            resolution_rule=str(decision.rule),
            resolver_model=decision.resolver_model,
            evidence_ids=tuple(decision.evidence_ids),
        )
        if decision.decision != "manual_required":
            if decision.decision == "obsolete":
                cursor = connection.execute(
                    "UPDATE conflict_cases SET status='resolved',decision='obsolete',resolved_at=? "
                    "WHERE id=? AND revision=? AND status IN ('pending','auto_resolved','manual_required')",
                    (now, case_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise StaleConflictDecision(f"stale conflict decision: {case_id}")
            elif decision.decision == "select_candidate":
                ResolutionService(connection).resolve_group(
                    case_id,
                    "select_candidate",
                    candidate_key=str(decision.winner_candidate_key),
                    expected_revision=expected_revision,
                    resolved_at=now,
                    rationale=str(decision.rule),
                    local_postconditions=True,
                )
            else:
                service = ResolutionService(connection)
                chosen_side = str(decision.decision).removeprefix("keep_")
                chosen_index = 0 if chosen_side == "left" else 1
                raw_endpoint = docket["case"].get(f"{chosen_side}_claim_id")
                followed_endpoint = docket["claims"][chosen_index].get("id")
                if decision.decision in {"keep_left", "keep_right"} and raw_endpoint != followed_endpoint:
                    service.resolve_followed_pair(
                        case_id,
                        str(decision.decision),
                        winner_id=str(followed_endpoint),
                        resolved_at=now,
                        rationale=str(decision.rule),
                        local_postconditions=True,
                    )
                else:
                    service.resolve(
                        case_id,
                        str(decision.decision),
                        resolved_at=now,
                        rationale=str(decision.rule),
                        expected_revision=expected_revision if docket["case"].get("group_key") else None,
                        local_postconditions=True,
                    )
        else:
            connection.execute(
                "UPDATE conflict_cases SET status='manual_required' WHERE id=? AND resolved_at IS NULL",
                (case_id,),
            )
        connection.execute(
            "UPDATE conflict_cases SET policy_version=?,last_tier=?,last_decision_hash=?,"
            "resolution_rule=?,resolver_model=? WHERE id=?",
            (
                policy_version,
                decision.tier,
                envelope.decision_hash,
                decision.rule,
                decision.resolver_model,
                case_id,
            ),
        )
        connection.execute(
            "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='reviewed_clean',"
            "last_reviewed_at=?,input_fingerprint=?,left_tip_id=?,right_tip_id=?,policy_version=? "
            "WHERE case_id=?",
            (
                now,
                expected_fingerprint,
                docket["context"]["left_tip_id"],
                docket["context"]["right_tip_id"],
                policy_version,
                case_id,
            ),
        )
        after = _mutation_snapshot(connection, case_id, claim_ids)
        GovernanceActionRepository(connection).record(
            envelope,
            before=before,
            after=after,
            status="applied",
            created_at=now,
            applied_at=now,
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {"case_id": case_id, "status": "applied", "decision": decision.decision, "tier": decision.tier}


def _ready_rows(connection: Any, now: str, max_cases: int) -> tuple[list[Any], int, int, str | None]:
    ready = (
        "state.dirty_at IS NOT NULL AND (state.not_before IS NULL OR state.not_before<=?) "
        "AND cases.status IN ('pending','auto_resolved','manual_required') AND cases.resolved_at IS NULL"
    )
    eligible = int(
        connection.execute(
            "SELECT count(*) FROM conflict_review_state state JOIN conflict_cases cases ON cases.id=state.case_id "
            f"WHERE {ready}",
            (now,),
        ).fetchone()[0]
    )
    blocked = int(
        connection.execute(
            "SELECT count(*) FROM conflict_review_state state JOIN conflict_cases cases ON cases.id=state.case_id "
            "WHERE state.dirty_at IS NOT NULL AND state.not_before>? "
            "AND cases.status IN ('pending','auto_resolved','manual_required') AND cases.resolved_at IS NULL",
            (now,),
        ).fetchone()[0]
    )
    oldest = connection.execute(
        "SELECT min(state.dirty_at) FROM conflict_review_state state JOIN conflict_cases cases "
        f"ON cases.id=state.case_id WHERE {ready}",
        (now,),
    ).fetchone()[0]
    prefix = (
        "SELECT state.case_id,state.dirty_at,state.attempt_count FROM conflict_review_state state "
        "JOIN conflict_cases cases ON cases.id=state.case_id "
    )
    cursor = connection.execute(
        "SELECT cursor_time,cursor_id FROM maintenance_cursors WHERE task='auto_resolve_conflicts'"
    ).fetchone()
    rows: list[Any] = []
    if cursor and cursor["cursor_time"] is not None:
        rows = connection.execute(
            prefix + f"WHERE {ready} AND (state.dirty_at,state.case_id)>(?,?) "
            "ORDER BY state.dirty_at,state.case_id LIMIT ?",
            (now, cursor["cursor_time"], cursor["cursor_id"], max_cases),
        ).fetchall()
    if len(rows) < max_cases:
        seen = {str(row["case_id"]) for row in rows}
        wrapped = connection.execute(
            prefix + f"WHERE {ready} ORDER BY state.dirty_at,state.case_id LIMIT ?",
            (now, max_cases),
        ).fetchall()
        rows.extend(row for row in wrapped if str(row["case_id"]) not in seen)
    return rows[:max_cases], eligible, blocked, str(oldest) if oldest else None


def _timestamp_after(now: str, seconds: int) -> str:
    return (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat()


def _record_failure(connection: Any, selected: Any, now: str, error: Exception, base_seconds: int) -> None:
    attempts = int(selected["attempt_count"] or 0) + 1
    delay = min(3_600, base_seconds * (2 ** min(attempts, 4)))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE conflict_review_state SET attempt_count=?,not_before=?,last_error=? "
            "WHERE case_id=? AND dirty_at IS NOT NULL",
            (
                attempts,
                _timestamp_after(now, delay),
                f"{type(error).__name__}: {str(error)[:512]}",
                selected["case_id"],
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _update_cursor(connection: Any, dirty_at: str, case_id: str, now: str) -> None:
    connection.execute(
        "INSERT INTO maintenance_cursors(task,cursor_time,cursor_id,updated_at) "
        "VALUES ('auto_resolve_conflicts',?,?,?) ON CONFLICT(task) DO UPDATE SET "
        "cursor_time=excluded.cursor_time,cursor_id=excluded.cursor_id,updated_at=excluded.updated_at",
        (dirty_at, case_id, now),
    )
    connection.commit()


def _age_seconds(oldest: str | None, now: str) -> float | None:
    if not oldest:
        return None
    try:
        return max(
            0.0,
            (
                datetime.fromisoformat(now.replace("Z", "+00:00"))
                - datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            ).total_seconds(),
        )
    except ValueError:
        return None


def auto_resolve_conflicts(
    connection: Any,
    now: str,
    *,
    max_cases: int = 50,
    max_elapsed_ms: int = 1_000,
    failure_backoff_seconds: int = 300,
    policy_version: str = CONFLICT_AUTO_POLICY_VERSION,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """有界消费 dirty case；确定性规则未命中时转人工裁决。"""

    if not 1 <= max_cases <= 1_000 or not 1 <= max_elapsed_ms <= 10_000:
        raise ValueError("invalid bounded conflict maintenance budget")
    if not 1 <= failure_backoff_seconds <= 86_400:
        raise ValueError("failure_backoff_seconds must be between 1 and 86400")
    upgrade_conflict_auto_policy(connection, now, policy_version)
    rows, eligible, blocked, oldest = _ready_rows(connection, now, max_cases)
    stats = {key: 0 for key in ("scanned", "changed", "resolved", "manual_stable", "deferred", "failed")}
    started = monotonic()
    last: tuple[str, str] | None = None
    for selected in rows:
        if stats["scanned"] and (monotonic() - started) * 1_000 >= max_elapsed_ms:
            break
        stats["scanned"] += 1
        last = str(selected["dirty_at"]), str(selected["case_id"])
        try:
            docket = load_conflict_docket(connection, str(selected["case_id"]))
            fingerprint = conflict_docket_fingerprint(docket)
            decision = decide_l0(docket)
            if decision is None:
                decision = AutoDecision(
                    "manual_required",
                    None,
                    0.0,
                    "L3",
                    "l0_only_manual_required",
                )
            apply_auto_conflict_decision(
                connection,
                str(selected["case_id"]),
                decision,
                expected_revision=int(docket["case"].get("revision") or 0),
                expected_fingerprint=fingerprint,
                policy_version=policy_version,
                now=now,
            )
            if decision.decision == "manual_required":
                stats["manual_stable"] += 1
                stats["deferred"] += 1
            else:
                stats["changed"] += 1
                stats["resolved"] += 1
        except Exception as error:
            stats["failed"] += 1
            stats["deferred"] += 1
            _record_failure(connection, selected, now, error, failure_backoff_seconds)
    if last:
        _update_cursor(connection, *last, now)
        assert_global_conflict_postconditions(connection)
    dirty_ready = int(
        connection.execute(
            "SELECT count(*) FROM conflict_review_state state JOIN conflict_cases cases ON cases.id=state.case_id "
            "WHERE state.dirty_at IS NOT NULL AND (state.not_before IS NULL OR state.not_before<=?) "
            "AND cases.status IN ('pending','auto_resolved','manual_required') AND cases.resolved_at IS NULL",
            (now,),
        ).fetchone()[0]
    )
    result = {
        **stats,
        "eligible": eligible,
        "auto_resolved": stats["resolved"],
        "manual_required": stats["manual_stable"],
        "budget_exhausted": stats["scanned"] < eligible,
        "cursor_time": last[0] if last else None,
        "cursor_id": last[1] if last else None,
        "elapsed_ms": max(0, int((monotonic() - started) * 1_000)),
        "dirty_ready": dirty_ready,
        "dirty_blocked": blocked,
        "oldest_dirty_age_seconds": _age_seconds(oldest, now),
    }
    return result
