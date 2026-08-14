"""组级冲突裁决应用服务。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from hl_mem.domain.claims.attributes import is_mutually_exclusive_attribute
from hl_mem.errors import ActiveClaimInvariantError, ConflictResolutionError
from hl_mem.lifecycle import assert_transition
from hl_mem.storage.claims import ClaimRepository

OPEN_CASE_STATUSES = ("pending", "auto_resolved", "manual_required")
NONTERMINAL_CLAIM_STATUSES = frozenset({"active", "candidate", "disputed"})
SUPPORTED_DECISIONS = frozenset({"keep_left", "keep_right", "coexist", "reject"})


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
    ) -> dict[str, Any]:
        if decision not in SUPPORTED_DECISIONS:
            raise ConflictResolutionError(f"unsupported conflict decision: {decision}")
        timestamp = resolved_at or datetime.now(timezone.utc).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            case = self._load_open_case(case_id)
            left = self._load_claim(str(case["left_claim_id"]))
            right = self._load_claim(str(case["right_claim_id"]))
            exclusive_group = self._is_exclusive_group(left, right)
            group_claims = self._load_group_claims(left, right, exclusive_group)

            if decision == "coexist" and exclusive_group:
                raise ConflictResolutionError(
                    "同互斥 conflict group 禁止 coexist；应共存需先修正 slot/qualifier 使脱离同 conflict key"
                )

            winner_id: str | None = None
            if decision in {"keep_left", "keep_right"}:
                winner_side = decision.removeprefix("keep_")
                winner_id = str(case[f"{winner_side}_claim_id"])
                self._converge_winner(group_claims, winner_id, timestamp)
                closed_case_ids = self._close_group_cases(group_claims, winner_id, timestamp)
                case_status = "resolved"
            elif decision == "coexist":
                for claim in (left, right):
                    self._activate_claim(claim)
                self._close_case(case, "resolved", "coexist", timestamp)
                closed_case_ids = [case_id]
                case_status = "resolved"
            else:
                self._close_case(case, "rejected", "reject", timestamp)
                closed_case_ids = [case_id]
                case_status = "rejected"

            if exclusive_group:
                self._assert_active_invariant(str(left["namespace_key"]), str(left["conflict_key"]))
            self.connection.commit()
        except Exception:
            if self.connection.in_transaction:
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

    def _load_open_case(self, case_id: str) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in OPEN_CASE_STATUSES)
        row = self.connection.execute(
            f"SELECT * FROM conflict_cases WHERE id=? AND status IN ({placeholders})",
            (case_id, *OPEN_CASE_STATUSES),
        ).fetchone()
        if row is None:
            raise ConflictResolutionError(f"open conflict case not found: {case_id}")
        return dict(row)

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

    def _close_group_cases(
        self,
        group_claims: list[dict[str, Any]],
        winner_id: str,
        timestamp: str,
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
            self._close_case(case, "resolved", case_decision, timestamp)
            closed_case_ids.append(str(case["id"]))
        return closed_case_ids

    def _close_case(self, case: dict[str, Any], status: str, decision: str, timestamp: str) -> None:
        cursor = self.connection.execute(
            "UPDATE conflict_cases SET status=?,decision=?,resolved_at=? WHERE id=? AND status=?",
            (status, decision, timestamp, case["id"], case["status"]),
        )
        if cursor.rowcount != 1:
            raise ConflictResolutionError(f"conflict case changed during resolution: {case['id']}")

    def _assert_active_invariant(self, namespace: str, conflict_key: str) -> None:
        active_count = int(
            self.connection.execute(
                "SELECT count(*) FROM claims WHERE namespace_key=? AND conflict_key=? AND status='active'",
                (namespace, conflict_key),
            ).fetchone()[0]
        )
        if active_count > 1:
            raise ActiveClaimInvariantError(
                f"conflict resolution left {active_count} active claims in group {namespace}:{conflict_key}"
            )
