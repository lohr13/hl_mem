"""组级冲突裁决应用服务。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from hl_mem.application.conflict_audit import CONFLICT_HUMAN_POLICY_VERSION, ConflictAuditWriter
from hl_mem.application.conflict_invariants import (
    assert_conflict_case_postconditions,
    assert_conflict_postconditions,
    assert_no_orphan_disputed_claims,
)
from hl_mem.application.conflict_queries import (
    OPEN_CASE_STATUSES,
    ConflictQueryService,
    follow_claim_tip,
    load_conflict_case,
)
from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.domain.governance import CONFLICT_AUTO_POLICY_VERSION
from hl_mem.errors import ConflictResolutionError
from hl_mem.lifecycle import assert_transition
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.governance import upgrade_conflict_auto_policy

NONTERMINAL_CLAIM_STATUSES = frozenset({"active", "candidate", "disputed"})
SUPPORTED_DECISIONS = frozenset({"keep_left", "keep_right", "coexist", "reject"})
DEFAULT_HUMAN_RESOLVER = "agent:hermes-local"
__all__ = [
    "CONFLICT_AUTO_POLICY_VERSION",
    "CONFLICT_HUMAN_POLICY_VERSION",
    "DEFAULT_HUMAN_RESOLVER",
    "ResolutionService",
    "StaleConflictDecision",
    "upgrade_conflict_auto_policy",
]


class StaleConflictDecision(ConflictResolutionError):
    """裁决期间 case revision 或输入指纹已经变化。"""


class ResolutionService:
    """在单一事务中裁决并收敛完整 conflict group。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def resolve(
        self,
        case_id: str,
        decision: str,
        *,
        resolved_at: str | None = None,
        rationale: str | None = None,
        expected_revision: int | None = None,
        resolver: str | None = None,
        local_postconditions: bool = False,
    ) -> dict[str, Any]:
        if decision not in SUPPORTED_DECISIONS:
            raise ConflictResolutionError(f"unsupported conflict decision: {decision}")
        timestamp = resolved_at or datetime.now(timezone.utc).isoformat()
        action_at = timestamp
        effective_rationale = rationale if rationale and rationale.strip() else None
        effective_resolver = self._normalize_resolver(resolver)
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            case = load_conflict_case(self.connection, case_id)
            if not (local_postconditions and expected_revision is None):
                self._assert_expected_revision(case, expected_revision)
            before_revision = int(case.get("revision") or 0)
            before_status = str(case["status"])
            left = self._load_claim(str(case["left_claim_id"]))
            right = self._load_claim(str(case["right_claim_id"]))
            exclusive_group = self._is_exclusive_group(left, right)
            group_claims = self._load_group_claims(left, right, exclusive_group)

            if decision == "coexist" and exclusive_group:
                raise ConflictResolutionError(
                    "同互斥 conflict group 禁止 coexist；应共存需先修正 slot/qualifier 使脱离同 conflict key"
                )
            if decision == "reject" and exclusive_group:
                raise ConflictResolutionError(
                    "同互斥 conflict group 禁止 reject；拒绝冲突需先修正 slot/qualifier 使脱离同 conflict key"
                )
            if decision == "reject":
                self._restore_rejected_pair(left, right)

            winner_id: str | None = None
            if case["status"] in {"resolved", "rejected"}:
                case_status, winner_id, closed_case_ids, timestamp = self._idempotent_terminal_result(
                    case,
                    decision,
                    group_claims,
                )
                if effective_rationale is not None:
                    self._update_terminal_group_rationale(case, group_claims, effective_rationale)
            elif decision in {"keep_left", "keep_right"}:
                winner_side = decision.removeprefix("keep_")
                winner_id = str(case[f"{winner_side}_claim_id"])
                self._converge_winner(group_claims, winner_id, timestamp)
                closed_case_ids = self._close_group_cases(
                    group_claims,
                    winner_id,
                    timestamp,
                    effective_rationale,
                )
                case_status = "resolved"
            elif decision == "coexist":
                for claim in (left, right):
                    self._activate_claim(claim)
                self._close_case(case, "resolved", "coexist", timestamp, effective_rationale)
                closed_case_ids = [case_id]
                case_status = "resolved"
            else:
                self._close_case(case, "rejected", "reject", timestamp, effective_rationale)
                closed_case_ids = [case_id]
                case_status = "rejected"

            namespace = str(left["namespace_key"]) if exclusive_group else None
            conflict_key = str(left["conflict_key"]) if exclusive_group else None
            if local_postconditions:
                assert_conflict_case_postconditions(
                    self.connection,
                    case_id=case_id,
                    namespace=namespace,
                    conflict_key=conflict_key,
                    touched_claim_ids=[str(claim["id"]) for claim in group_claims],
                )
            else:
                assert_conflict_postconditions(
                    self.connection,
                    namespace=namespace,
                    conflict_key=conflict_key,
                )
            if decision == "reject" and not local_postconditions:
                assert_no_orphan_disputed_claims(self.connection)
            if effective_resolver is not None:
                current = self.connection.execute(
                    "SELECT revision,status FROM conflict_cases WHERE id=?",
                    (case_id,),
                ).fetchone()
                ConflictAuditWriter(self.connection).record_human_action(
                    case_id=case_id,
                    decision=decision,
                    candidate_key=None,
                    rationale=effective_rationale,
                    resolver=effective_resolver,
                    before_revision=before_revision,
                    after_revision=int(current["revision"]),
                    before_status=before_status,
                    after_status=str(current["status"]),
                    timestamp=action_at,
                )
            if owns_transaction:
                self.connection.commit()
        except Exception:
            if owns_transaction and self.connection.in_transaction:
                self.connection.rollback()
            raise
        return {
            "id": case_id,
            "status": case_status,
            "decision": decision,
            "resolved_at": timestamp,
            "winner_id": winner_id,
            "closed_case_ids": closed_case_ids,
        }

    def resolve_followed_pair(
        self,
        case_id: str,
        decision: str,
        *,
        winner_id: str,
        resolved_at: str,
        rationale: str,
        local_postconditions: bool = False,
    ) -> None:
        """Resolve a legacy pair against its current supersession-chain tips."""

        if decision not in {"keep_left", "keep_right"}:
            raise ConflictResolutionError(f"unsupported followed-tip decision: {decision}")
        case = load_conflict_case(self.connection, case_id)
        if case.get("group_key") is not None:
            raise ConflictResolutionError(f"followed-tip pair unexpectedly has a group key: {case_id}")
        repository = ClaimRepository(self.connection)
        left = follow_claim_tip(repository, str(case["left_claim_id"]))
        right = follow_claim_tip(repository, str(case["right_claim_id"]))
        if left is None or right is None or winner_id not in {left["id"], right["id"]}:
            raise ConflictResolutionError(f"followed conflict endpoint changed: {case_id}")
        self._converge_winner([left, right], winner_id, resolved_at)
        self._close_case(case, "resolved", decision, resolved_at, rationale)
        if local_postconditions:
            assert_conflict_case_postconditions(
                self.connection,
                case_id=case_id,
                namespace=None,
                conflict_key=None,
                touched_claim_ids=[str(left["id"]), str(right["id"])],
            )
        else:
            assert_conflict_postconditions(self.connection)

    def review(self, case_id: str) -> dict[str, Any]:
        """返回一个 revision 快照及其全部 canonical candidates。"""

        return ConflictQueryService(self.connection).review(case_id)

    def resolve_group(
        self,
        case_id: str,
        action: str,
        *,
        candidate_key: str,
        expected_revision: int,
        resolved_at: str | None = None,
        rationale: str | None = None,
        resolver: str | None = None,
        local_postconditions: bool = False,
    ) -> dict[str, Any]:
        """以乐观 revision guard 选择或拒绝一个 group candidate。"""

        if action not in {"select_candidate", "reject_candidate"}:
            raise ConflictResolutionError(f"unsupported group conflict action: {action}")
        timestamp = resolved_at or datetime.now(timezone.utc).isoformat()
        effective_rationale = rationale if rationale and rationale.strip() else None
        effective_resolver = self._normalize_resolver(resolver)
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            case = load_conflict_case(self.connection, case_id)
            if case.get("group_key") is None or case.get("namespace_key") is None:
                raise ConflictResolutionError(f"conflict case is not group-native: {case_id}")
            self._assert_expected_revision(case, expected_revision)
            before_revision = int(case.get("revision") or 0)
            before_status = str(case["status"])
            candidate = self.connection.execute(
                "SELECT * FROM conflict_case_candidates WHERE case_id=? AND candidate_key=?",
                (case_id, candidate_key),
            ).fetchone()
            if candidate is None:
                raise ConflictResolutionError(f"conflict candidate not found: {case_id}:{candidate_key}")
            winner_id: str | None = None
            terminal_replay = case["status"] in {"resolved", "rejected"}
            if terminal_replay:
                if (
                    action != "select_candidate"
                    or case["status"] != "resolved"
                    or case.get("decision") != "select_candidate"
                    or not case.get("resolved_at")
                ):
                    raise ConflictResolutionError(
                        f"terminal conflict case has a different group decision: {case_id} ({case.get('decision')})"
                    )
                left = self._load_claim(str(case["left_claim_id"]))
                right = self._load_claim(str(case["right_claim_id"]))
                group_claims = self._load_group_claims(left, right, True)
                winner_id = self._established_group_winner(case, group_claims)
                if str(candidate["representative_claim_id"]) != winner_id:
                    raise ConflictResolutionError(
                        f"terminal conflict case has a different group winner: {case_id} ({winner_id})"
                    )
                status = "resolved"
            elif action == "select_candidate":
                winner_id = str(candidate["representative_claim_id"])
                left = self._load_claim(str(case["left_claim_id"]))
                right = self._load_claim(str(case["right_claim_id"]))
                group_claims = self._load_group_claims(left, right, True)
                self._converge_winner(group_claims, winner_id, timestamp)
                self._close_case(
                    case,
                    "resolved",
                    "select_candidate",
                    timestamp,
                    effective_rationale,
                )
                status = "resolved"
            else:
                member_rows = self.connection.execute(
                    "SELECT claim_id FROM conflict_candidate_members "
                    "WHERE case_id=? AND candidate_key=? ORDER BY claim_id",
                    (case_id, candidate_key),
                ).fetchall()
                self.connection.execute(
                    "DELETE FROM conflict_case_candidates WHERE case_id=? AND candidate_key=?",
                    (case_id, candidate_key),
                )
                for member_row in member_rows:
                    claim = self._load_claim(str(member_row["claim_id"]))
                    if claim.get("status") not in NONTERMINAL_CLAIM_STATUSES:
                        continue
                    assert_transition(str(claim["status"]), "retracted")
                    cursor = self.connection.execute(
                        "UPDATE claims SET status='retracted',valid_to=?,recorded_to=? " "WHERE id=? AND status=?",
                        (timestamp, timestamp, claim["id"], claim["status"]),
                    )
                    if cursor.rowcount != 1:
                        raise ConflictResolutionError(f"conflict candidate member changed: {claim['id']}")
                status = "manual_required"
            if terminal_replay:
                pass
            elif local_postconditions:
                assert_conflict_case_postconditions(
                    self.connection,
                    case_id=case_id,
                    namespace=str(case["namespace_key"]),
                    conflict_key=str(case["group_key"]),
                    touched_claim_ids=(
                        [str(claim["id"]) for claim in group_claims]
                        if action == "select_candidate"
                        else [str(row["claim_id"]) for row in member_rows]
                    ),
                )
            else:
                assert_conflict_postconditions(
                    self.connection,
                    namespace=str(case["namespace_key"]),
                    conflict_key=str(case["group_key"]),
                )
            current = self.connection.execute(
                "SELECT revision,status,resolved_at FROM conflict_cases WHERE id=?",
                (case_id,),
            ).fetchone()
            if effective_resolver is not None and not terminal_replay:
                ConflictAuditWriter(self.connection).record_human_action(
                    case_id=case_id,
                    decision=action,
                    candidate_key=candidate_key,
                    rationale=effective_rationale,
                    resolver=effective_resolver,
                    before_revision=before_revision,
                    after_revision=int(current["revision"]),
                    before_status=before_status,
                    after_status=str(current["status"]),
                    timestamp=timestamp,
                )
            if owns_transaction:
                self.connection.commit()
        except Exception:
            if owns_transaction and self.connection.in_transaction:
                self.connection.rollback()
            raise
        return {
            "case_id": case_id,
            "generation": int(case["generation"]),
            "revision": int(current["revision"]),
            "status": status,
            "action": action,
            "candidate_key": candidate_key,
            "winner_id": winner_id,
            "resolved_at": current["resolved_at"],
        }

    @staticmethod
    def _assert_expected_revision(case: dict[str, Any], expected_revision: int | None) -> None:
        current_revision = int(case.get("revision") or 0)
        if expected_revision is None:
            raise ConflictResolutionError(f"expected_revision is required for conflict case: {case['id']}")
        if expected_revision != current_revision:
            raise ConflictResolutionError(
                f"stale conflict revision: expected {expected_revision}, current {current_revision}"
            )

    @staticmethod
    def _normalize_resolver(resolver: str | None) -> str | None:
        if resolver is None:
            return None
        normalized = resolver.strip()
        if not normalized:
            raise ConflictResolutionError("resolver must not be empty")
        if len(normalized) > 200:
            raise ConflictResolutionError("resolver must not exceed 200 characters")
        return normalized

    def _idempotent_terminal_result(
        self,
        case: dict[str, Any],
        decision: str,
        group_claims: list[dict[str, Any]],
    ) -> tuple[str, str | None, list[str], str]:
        if (
            case["status"] == "resolved"
            and case.get("decision") == "group_winner"
            and case.get("resolved_at")
            and decision in {"keep_left", "keep_right"}
        ):
            established_winner_id = self._established_group_winner(case, group_claims)
            return "resolved", established_winner_id, [str(case["id"])], str(case["resolved_at"])
        expected_status = "rejected" if decision == "reject" else "resolved"
        if case["status"] != expected_status or case.get("decision") != decision or not case.get("resolved_at"):
            raise ConflictResolutionError(
                f"terminal conflict case has a different decision: {case['id']} ({case.get('decision')})"
            )
        winner_id: str | None = None
        if decision in {"keep_left", "keep_right"}:
            winner_id = str(case[f"{decision.removeprefix('keep_')}_claim_id"])
        return str(case["status"]), winner_id, [str(case["id"])], str(case["resolved_at"])

    @staticmethod
    def _established_group_winner(case: dict[str, Any], group_claims: list[dict[str, Any]]) -> str:
        claims_by_id = {str(claim["id"]): claim for claim in group_claims}

        def terminal_claim_id(start_id: str) -> str:
            claim_id = start_id
            visited: set[str] = set()
            while True:
                if claim_id in visited:
                    raise ConflictResolutionError(f"supersession cycle in conflict group: {claim_id}")
                visited.add(claim_id)
                claim = claims_by_id.get(claim_id)
                if claim is None:
                    raise ConflictResolutionError(f"conflict group winner is missing: {claim_id}")
                successor_id = claim.get("superseded_by_id")
                if not successor_id:
                    if claim.get("status") != "active":
                        raise ConflictResolutionError(f"conflict group has no established active winner: {claim_id}")
                    return claim_id
                claim_id = str(successor_id)

        left_winner = terminal_claim_id(str(case["left_claim_id"]))
        right_winner = terminal_claim_id(str(case["right_claim_id"]))
        if left_winner != right_winner:
            raise ConflictResolutionError(
                f"terminal group_winner case does not converge: {case['id']} ({left_winner}, {right_winner})"
            )
        return left_winner

    def _load_claim(self, claim_id: str) -> dict[str, Any]:
        claim = ClaimRepository(self.connection).get_claim(claim_id)
        if claim is None:
            raise ConflictResolutionError(f"conflict case references missing claim: {claim_id}")
        return claim

    @staticmethod
    def _is_exclusive_group(left: dict[str, Any], right: dict[str, Any]) -> bool:
        conflict_key = left.get("conflict_key")
        return bool(
            conflict_key
            and conflict_key == right.get("conflict_key")
            and left.get("namespace_key") == right.get("namespace_key")
            and is_mutually_exclusive_attribute(left.get("canonical_slot"))
            and is_mutually_exclusive_attribute(right.get("canonical_slot"))
        )

    def _load_group_claims(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        exclusive_group: bool,
    ) -> list[dict[str, Any]]:
        if not exclusive_group:
            return [left, right]
        rows = self.connection.execute(
            "SELECT id FROM claims WHERE namespace_key=? AND conflict_key=? ORDER BY recorded_from,id",
            (left["namespace_key"], left["conflict_key"]),
        ).fetchall()
        repository = ClaimRepository(self.connection)
        claims = [repository.get_claim(str(row["id"])) for row in rows]
        return [claim for claim in claims if claim is not None]

    def _converge_winner(self, group_claims: list[dict[str, Any]], winner_id: str, timestamp: str) -> None:
        winner = next((claim for claim in group_claims if claim["id"] == winner_id), None)
        if winner is None or winner.get("status") not in NONTERMINAL_CLAIM_STATUSES:
            raise ConflictResolutionError(f"conflict group winner is not resolvable: {winner_id}")
        repository = ClaimRepository(self.connection)
        for claim in group_claims:
            if claim["id"] == winner_id or claim.get("status") not in NONTERMINAL_CLAIM_STATUSES:
                continue
            result = repository.supersede_with_inline(
                claim["id"],
                winner_id,
                winner.get("value"),
                timestamp,
                timestamp,
                commit=False,
            )
            if not result.applied:
                raise ConflictResolutionError(f"conflict group member changed during resolution: {claim['id']}")
        self._activate_claim(winner)

    def _activate_claim(self, claim: dict[str, Any]) -> None:
        status = str(claim.get("status"))
        if status == "active":
            return
        if status not in {"candidate", "disputed"}:
            raise ConflictResolutionError(f"claim cannot be activated from terminal status: {claim['id']} ({status})")
        assert_transition(status, "active")
        cursor = self.connection.execute(
            "UPDATE claims SET status='active' WHERE id=? AND status=?",
            (claim["id"], status),
        )
        if cursor.rowcount != 1:
            raise ConflictResolutionError(f"claim changed during resolution: {claim['id']}")

    def _restore_rejected_pair(self, left: dict[str, Any], right: dict[str, Any]) -> None:
        for claim in (left, right):
            if claim.get("status") in NONTERMINAL_CLAIM_STATUSES:
                self._activate_claim(claim)

    def _close_group_cases(
        self,
        group_claims: list[dict[str, Any]],
        winner_id: str,
        timestamp: str,
        rationale: str | None,
    ) -> list[str]:
        claim_ids = [str(claim["id"]) for claim in group_claims]
        placeholders = ",".join("?" for _ in claim_ids)
        status_placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
        rows = self.connection.execute(
            "SELECT * FROM conflict_cases "
            f"WHERE left_claim_id IN ({placeholders}) AND right_claim_id IN ({placeholders}) "
            f"AND status IN ({status_placeholders}) ORDER BY id",
            (*claim_ids, *claim_ids, *OPEN_CASE_STATUSES),
        ).fetchall()
        closed_case_ids: list[str] = []
        for row in rows:
            case = dict(row)
            if case["left_claim_id"] == winner_id:
                case_decision = "keep_left"
            elif case["right_claim_id"] == winner_id:
                case_decision = "keep_right"
            else:
                case_decision = "group_winner"
            self._close_case(case, "resolved", case_decision, timestamp, rationale)
            closed_case_ids.append(str(case["id"]))
        return closed_case_ids

    def _update_terminal_group_rationale(
        self,
        case: dict[str, Any],
        group_claims: list[dict[str, Any]],
        rationale: str,
    ) -> None:
        claim_ids = [str(claim["id"]) for claim in group_claims]
        placeholders = ",".join("?" for _ in claim_ids)
        cursor = self.connection.execute(
            "UPDATE conflict_cases SET rationale=? "
            f"WHERE left_claim_id IN ({placeholders}) AND right_claim_id IN ({placeholders}) "
            "AND status IN ('resolved','rejected') AND resolved_at=?",
            (rationale, *claim_ids, *claim_ids, case["resolved_at"]),
        )
        if cursor.rowcount < 1:
            raise ConflictResolutionError(f"terminal conflict group changed during rationale update: {case['id']}")

    def _close_case(
        self,
        case: dict[str, Any],
        status: str,
        decision: str,
        timestamp: str,
        rationale: str | None,
    ) -> None:
        if rationale is None:
            cursor = self.connection.execute(
                "UPDATE conflict_cases SET status=?,decision=?,resolved_at=? WHERE id=? AND status=?",
                (status, decision, timestamp, case["id"], case["status"]),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE conflict_cases SET status=?,decision=?,resolved_at=?,rationale=? " "WHERE id=? AND status=?",
                (status, decision, timestamp, rationale, case["id"], case["status"]),
            )
        if cursor.rowcount != 1:
            raise ConflictResolutionError(f"conflict case changed during resolution: {case['id']}")
