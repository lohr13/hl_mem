"""冲突案卷、CAS 应用、有界维护入口及领域法条兼容导出。"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from hl_mem.application.conflict_invariants import assert_global_conflict_postconditions
from hl_mem.application.conflicts import (
    ResolutionService,
    StaleConflictDecision,
    _follow_tip,
)
from hl_mem.domain.claims.conflicts import coordinate_qualifier_key
from hl_mem.domain.governance import (
    CONFLICT_AUTO_POLICY_VERSION,
    AutoDecision,
    DecisionEnvelope,
    assess_l2_admission,
    decide_l0,
    is_terminal_conflict_status,
    snapshot_fingerprint,
    validate_l2_result,
)
from hl_mem.errors import ConflictResolutionError
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.governance import (
    GovernanceActionRepository,
    GovernanceStatus,
    upgrade_conflict_auto_policy,
)
from hl_mem.storage.jobs import JobRepository

__all__ = [
    "AutoDecision",
    "StaleConflictDecision",
    "assess_l2_admission",
    "auto_resolve_conflicts",
    "decide_l0",
    "validate_l2_result",
]


def _coordinates_complete(left: dict[str, Any], right: dict[str, Any]) -> bool:
    slot = left.get("canonical_slot")
    return bool(
        isinstance(slot, str)
        and slot == right.get("canonical_slot")
        and left.get("namespace_key") == right.get("namespace_key")
        and left.get("subject_entity_id")
        and left.get("subject_entity_id") == right.get("subject_entity_id")
        and coordinate_qualifier_key(slot, left.get("qualifiers"))
        == coordinate_qualifier_key(slot, right.get("qualifiers"))
    )


def _claim_evidence(connection: sqlite3.Connection, claim_ids: list[str]) -> tuple[list[dict[str, Any]], bool]:
    placeholders = ",".join("?" for _ in claim_ids)
    rows = connection.execute(
        "SELECT id,derived_id,evidence_type,evidence_id,relation,weight FROM evidence_links "
        f"WHERE derived_type='claim' AND derived_id IN ({placeholders}) ORDER BY id",
        claim_ids,
    ).fetchall()
    evidence = [dict(row) for row in rows]
    readable = True
    for row in evidence:
        table = "events" if row["evidence_type"] == "event" else "claims"
        if (
            row["evidence_type"] not in {"event", "claim"}
            or connection.execute(f"SELECT 1 FROM {table} WHERE id=?", (row["evidence_id"],)).fetchone() is None
        ):
            readable = False
    return evidence, readable


def load_conflict_docket(connection: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    """读取一次裁决所需的完整 pair/group 快照，不开启写事务。"""

    row = connection.execute(
        "SELECT cases.*,state.dirty_at,state.dirty_reason,state.not_before,state.input_fingerprint,"
        "state.policy_version AS review_policy_version FROM conflict_cases AS cases "
        "LEFT JOIN conflict_review_state AS state ON state.case_id=cases.id WHERE cases.id=?",
        (case_id,),
    ).fetchone()
    if row is None:
        raise ConflictResolutionError(f"conflict case not found: {case_id}")
    case = dict(row)
    repository = ClaimRepository(connection)
    left = _follow_tip(repository, str(case["left_claim_id"]))
    right = _follow_tip(repository, str(case["right_claim_id"]))
    if left is None or right is None:
        raise ConflictResolutionError(f"conflict case has invalid endpoints: {case_id}")
    claim_ids = [str(left["id"]), str(right["id"])]
    evidence, evidence_readable = _claim_evidence(connection, claim_ids)
    review = ResolutionService(connection).review(case_id)
    candidates = review["candidates"]
    group_native = bool(case.get("group_key") and candidates)
    if not candidates:
        counts = {claim_id: 0 for claim_id in claim_ids}
        for item in evidence:
            counts[str(item["derived_id"])] += 1
        candidates = [
            {
                "candidate_key": claim_id,
                "representative_claim_id": claim_id,
                "support_count": 1,
                "evidence_count": counts[claim_id],
                "terminal": is_terminal_conflict_status(claim.get("status")),
            }
            for claim_id, claim in zip(claim_ids, (left, right), strict=True)
        ]
    else:
        for candidate in candidates:
            statuses = candidate.get("claim_statuses") or {}
            candidate["terminal"] = bool(statuses) and all(
                is_terminal_conflict_status(status) for status in statuses.values()
            )
    case["group_native"] = group_native
    other_open = connection.execute(
        "SELECT 1 FROM conflict_cases WHERE id<>? AND status IN ('pending','auto_resolved','manual_required') "
        "AND resolved_at IS NULL AND (left_claim_id IN (?,?) OR right_claim_id IN (?,?)) LIMIT 1",
        (case_id, *claim_ids, *claim_ids),
    ).fetchone()
    context = {
        "left_tip_id": left["id"],
        "right_tip_id": right["id"],
        "survivor_contested": other_open is not None,
        "schema_valid": bool(case.get("namespace_key") or left.get("namespace_key")),
        "evidence_readable": evidence_readable,
        "entity_type_mismatch": False,
        "coordinates_complete": _coordinates_complete(left, right),
        "equal_authority_first_hand_conflict": False,
        "previous_reason": None,
        "last_l2_policy_version": case.get("policy_version") if case.get("last_tier") in {"L2", "L3"} else None,
        "not_before": case.get("not_before"),
        "docket_oversized": False,
        "nonexclusive_false_positive": bool(
            left.get("canonical_slot") != right.get("canonical_slot")
            or (
                left.get("valid_from")
                and left.get("valid_from") == right.get("valid_from")
                and not _coordinates_complete(left, right)
            )
        ),
    }
    return {"case": case, "claims": [left, right], "candidates": candidates, "evidence": evidence, "context": context}


def conflict_docket_fingerprint(docket: dict[str, Any]) -> str:
    """散列实际裁决读取的 case/claim/candidate/evidence 有界字段。"""

    case = docket["case"]
    claim_fields = (
        "id",
        "status",
        "namespace_key",
        "subject_entity_id",
        "canonical_slot",
        "value_json",
        "qualifiers_json",
        "valid_from",
        "valid_to",
        "recorded_from",
        "source_authority",
        "confidence",
        "assertion_kind",
        "superseded_by_id",
    )
    payload = {
        "case": {key: case.get(key) for key in ("id", "generation", "revision", "overflow", "group_key")},
        "claims": [{key: claim.get(key) for key in claim_fields} for claim in docket["claims"]],
        "candidates": docket["candidates"],
        "evidence_ids": [item.get("id") for item in docket["evidence"]],
    }
    return snapshot_fingerprint(payload)


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
    mode: str,
    now: str,
) -> dict[str, Any]:
    """重读 revision/fingerprint，并在单一短事务中应用 mutation 与 ledger。"""

    if mode not in {"observe", "enforce"}:
        raise ValueError("conflict auto application mode must be observe or enforce")
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
        if mode == "enforce" and decision.decision not in {"manual_required"}:
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
        elif decision.decision == "manual_required":
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
        action_status: GovernanceStatus = "applied" if mode == "enforce" else "observed"
        GovernanceActionRepository(connection).record(
            envelope,
            before=before,
            after=after,
            status=action_status,
            created_at=now,
            applied_at=now if action_status == "applied" else None,
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return {"case_id": case_id, "status": action_status, "decision": decision.decision, "tier": decision.tier}


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


def _enqueue_l2(
    connection: Any,
    docket: dict[str, Any],
    fingerprint: str,
    now: str,
    policy_version: str,
    application_mode: str,
) -> bool:
    case = docket["case"]
    key = f"resolve_conflict_llm:{case['id']}:{fingerprint}:{policy_version}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        inserted = JobRepository(connection).insert_job(
            {
                "id": uuid.uuid4().hex,
                "job_type": "resolve_conflict_llm",
                "payload": {
                    "case_id": case["id"],
                    "revision": int(case.get("revision") or 0),
                    "input_fingerprint": fingerprint,
                    "policy_version": policy_version,
                    "application_mode": application_mode,
                },
                "idempotency_key": key,
                "status": "pending",
                "run_after": now,
                "max_attempts": 3,
                "created_at": now,
                "updated_at": now,
            },
            commit=False,
        )
        connection.execute(
            "UPDATE conflict_review_state SET dirty_at=NULL,dirty_reason='l2_queued',last_reviewed_at=?,"
            "input_fingerprint=?,left_tip_id=?,right_tip_id=?,policy_version=? WHERE case_id=?",
            (
                now,
                fingerprint,
                docket["context"]["left_tip_id"],
                docket["context"]["right_tip_id"],
                policy_version,
                case["id"],
            ),
        )
        connection.commit()
        return bool(inserted)
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


def _application_mode(mode: str, tier: str) -> str:
    if mode == "enforce" or (mode == "l0_only" and tier == "L0"):
        return "enforce"
    return "observe"


def auto_resolve_conflicts(
    connection: Any,
    now: str,
    *,
    max_cases: int = 50,
    max_elapsed_ms: int = 1_000,
    failure_backoff_seconds: int = 300,
    mode: str = "enforce",
    max_candidates: int = 8,
    policy_version: str = CONFLICT_AUTO_POLICY_VERSION,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """有界消费 dirty case；L2 只入 job，不在 maintenance 调模型。"""

    if mode == "off":
        return {"eligible": 0, "scanned": 0, "changed": 0, "resolved": 0, "l2_queued": 0}
    if mode not in {"observe", "enforce", "l0_only"}:
        raise ValueError("mode must be off, observe, enforce, or l0_only")
    if not 1 <= max_cases <= 1_000 or not 1 <= max_elapsed_ms <= 10_000:
        raise ValueError("invalid bounded conflict maintenance budget")
    if not 1 <= failure_backoff_seconds <= 86_400:
        raise ValueError("failure_backoff_seconds must be between 1 and 86400")
    upgrade_conflict_auto_policy(connection, now, policy_version)
    rows, eligible, blocked, oldest = _ready_rows(connection, now, max_cases)
    stats = {key: 0 for key in ("scanned", "changed", "resolved", "manual_stable", "deferred", "failed", "l2_queued")}
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
                if mode == "l0_only":
                    decision = AutoDecision(
                        "manual_required",
                        None,
                        0.0,
                        "L3",
                        "l0_only_manual_required",
                    )
                else:
                    docket["context"]["previous_reason"] = "l0_insufficient"
                    admission = assess_l2_admission(
                        docket,
                        now,
                        max_candidates=max_candidates,
                        policy_version=policy_version,
                    )
                    if admission.admitted:
                        stats["l2_queued"] += int(
                            _enqueue_l2(
                                connection,
                                docket,
                                fingerprint,
                                now,
                                policy_version,
                                _application_mode(mode, "L2"),
                            )
                        )
                        stats["manual_stable"] += 1
                        stats["deferred"] += 1
                        continue
                    decision = AutoDecision("manual_required", None, 0.0, "L3", admission.reason)
            application_mode = _application_mode(mode, str(decision.tier))
            result = apply_auto_conflict_decision(
                connection,
                str(selected["case_id"]),
                decision,
                expected_revision=int(docket["case"].get("revision") or 0),
                expected_fingerprint=fingerprint,
                policy_version=policy_version,
                mode=application_mode,
                now=now,
            )
            if decision.decision == "manual_required":
                stats["manual_stable"] += 1
                stats["deferred"] += 1
            elif application_mode == "enforce":
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
