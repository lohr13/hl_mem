"""Transactional plan fulfillment with idempotent outcomes and conditional rollback."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Mapping, cast

from hl_mem.domain.action_coordinates import decimal_text
from hl_mem.domain.governance import DecisionEnvelope, canonical_snapshot, snapshot_fingerprint
from hl_mem.domain.plan_fulfillment import (
    PLAN_FULFILLMENT_POLICY_VERSION,
    PlanMatch,
    coordinate_hash,
    select_plan_match,
)
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.governance import GovernanceActionRepository, StaleGovernanceAction
from hl_mem.storage.plan_fulfillments import PlanFulfillmentRepository

_RELATIONS = {
    "complete": "fulfilled_by",
    "partial": "partially_fulfilled_by",
    "cancel": "cancelled_by",
    "replace": "replaced_by",
}
PlanMode = Literal["off", "audit", "observe", "enforce"]


@dataclass(slots=True)
class _Prepared:
    result: dict[str, Any]
    match: PlanMatch
    fingerprint: str
    cumulative: Decimal | None
    close_at: str | None


class PlanFulfillmentService:
    def __init__(self, connection: sqlite3.Connection, *, mode: str | None = None) -> None:
        self.connection = connection
        configured = getattr(connection, "hl_mem_settings", None)
        resolved_mode = mode or getattr(configured, "plan_fulfillment_mode", "audit")
        if resolved_mode not in {"off", "audit", "observe", "enforce"}:
            raise ValueError("invalid plan fulfillment mode")
        self.mode = cast(PlanMode, resolved_mode)
        self.repo = PlanFulfillmentRepository(connection)

    def _existing_response(self, result_claim_id: str) -> dict[str, Any] | None:
        rows = self.repo.list_for_result(result_claim_id, PLAN_FULFILLMENT_POLICY_VERSION)
        if not rows:
            return None
        primary = next((row for row in rows if row["outcome_type"] == "partial"), rows[0])
        response = {
            "status": primary["status"],
            "outcome_type": "ambiguous" if primary["status"] == "ambiguous" else primary["outcome_type"],
            "reason": primary["match_rule"],
            "plan_claim_id": primary["plan_claim_id"],
            "cumulative_quantity_text": primary["cumulative_quantity_text"],
        }
        action_id = self.repo.action_id(
            str(primary["plan_claim_id"]),
            str(primary["input_fingerprint"]),
            PLAN_FULFILLMENT_POLICY_VERSION,
        )
        if action_id:
            response["action_id"] = action_id
        return response

    def _prepare(self, result_claim_id: str) -> _Prepared | dict[str, Any]:
        result = ClaimRepository(self.connection).get_claim(result_claim_id)
        if result is None:
            raise KeyError(result_claim_id)
        candidates = self.repo.find_open_plans(result)
        pairs = self.repo.equivalent_pairs(str(item["id"]) for item in candidates)
        match = select_plan_match(candidates, result, pairs)
        if match is None:
            return {"status": "ambiguous", "reason": "coordinate_incomplete_or_no_match"}
        if not match.plan_ids:
            return {"status": "ambiguous", "reason": match.reason}
        previous = self.repo.applied_partials(match.plan_ids[0], PLAN_FULFILLMENT_POLICY_VERSION)
        previous_total = sum((Decimal(str(row["matched_quantity_text"])) for row in previous), Decimal(0))
        cumulative = previous_total + match.quantity if match.quantity is not None else None
        close_at = str(result.get("valid_from") or "") or None
        plan_amount = match.coordinate.quantity.amount
        if match.outcome_type in {"partial", "complete"} and plan_amount is not None and cumulative is not None:
            if cumulative > plan_amount:
                match = PlanMatch(match.plan_ids, match.coordinate, "ambiguous", "partial_overfill", match.quantity)
            elif cumulative < plan_amount:
                match = PlanMatch(
                    match.plan_ids, match.coordinate, "partial", "strict_partial_execution", match.quantity
                )
            elif previous:
                match = PlanMatch(match.plan_ids, match.coordinate, "partial", "partial_reaches_total", match.quantity)
                close_at = max([str(row["result_valid_from"]) for row in previous] + [str(close_at)])
        fingerprint = snapshot_fingerprint(
            {
                "policy": PLAN_FULFILLMENT_POLICY_VERSION,
                "result": _claim_input(result),
                "plans": [_claim_input(item) for item in candidates if str(item["id"]) in match.plan_ids],
                "previous_partials": [
                    (row["id"], row["matched_quantity_text"], row["result_valid_from"]) for row in previous
                ],
            }
        )
        return _Prepared(result, match, fingerprint, cumulative, close_at)

    def reconcile(self, result_claim_id: str, *, now: str) -> dict[str, Any]:
        """Reconcile one result in a bounded write transaction; no model is called here."""

        if self.mode == "off":
            return {"status": "off", "reason": "plan_fulfillment_disabled"}
        existing = self._existing_response(result_claim_id)
        if existing is not None and existing["status"] not in {"candidate", "observed"}:
            return existing
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prepared = self._prepare(result_claim_id)
            if isinstance(prepared, dict):
                self.connection.commit()
                return prepared
            response = self._persist(prepared, now)
            self.connection.commit()
            return response
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def _persist(self, prepared: _Prepared, now: str) -> dict[str, Any]:
        match = prepared.match
        status = {"audit": "candidate", "observe": "observed", "enforce": "applied"}[self.mode]
        if match.outcome_type == "ambiguous":
            status = "ambiguous"
        plan_before = _plan_state(self.connection, match.plan_ids)
        relation_ids: list[str] = []
        outcome_ids: list[str] = []
        terminal = match.outcome_type in {"complete", "cancel", "replace"}
        reaches_total = match.reason == "partial_reaches_total"
        if self.mode == "enforce" and status == "applied" and (terminal or reaches_total):
            self.connection.executemany(
                "UPDATE claims SET valid_to=? WHERE id=? AND valid_to IS NULL",
                [(prepared.close_at, plan_id) for plan_id in match.plan_ids],
            )
        for plan_id in match.plan_ids:
            relation_id = None
            if self.mode == "enforce" and status == "applied":
                relation = _RELATIONS.get(match.outcome_type)
                if relation is None:
                    raise ValueError(f"unsupported applied plan outcome: {match.outcome_type}")
                relation_id = self.repo.add_relation(plan_id, str(prepared.result["id"]), relation, now)
                relation_ids.append(relation_id)
            outcome = self.repo.insert_outcome(_outcome_values(prepared, plan_id, status, relation_id, now))
            outcome_ids.append(str(outcome["id"]))
            if self.mode == "enforce" and status == "applied" and reaches_total:
                complete_relation = self.repo.add_relation(
                    plan_id, str(prepared.result["id"]), _RELATIONS["complete"], now
                )
                relation_ids.append(complete_relation)
                complete = self.repo.insert_outcome(
                    _outcome_values(prepared, plan_id, status, complete_relation, now, "complete")
                )
                outcome_ids.append(str(complete["id"]))
        action_id = None
        if self.mode in {"observe", "enforce"} and status != "ambiguous":
            action_id = self._record_action(prepared, plan_before, outcome_ids, relation_ids, now)
        response = {
            "status": status,
            "outcome_type": match.outcome_type,
            "reason": match.reason,
            "plan_claim_id": match.plan_ids[0],
            "cumulative_quantity_text": decimal_text(prepared.cumulative) if prepared.cumulative else None,
        }
        if action_id:
            response["action_id"] = action_id
        return response

    def _record_action(
        self,
        prepared: _Prepared,
        before: Mapping[str, Any],
        outcome_ids: list[str],
        relation_ids: list[str],
        now: str,
    ) -> str:
        after = _action_state(self.connection, prepared.match.plan_ids, outcome_ids, relation_ids)
        existing_id = self.repo.action_id(
            prepared.match.plan_ids[0],
            prepared.fingerprint,
            PLAN_FULFILLMENT_POLICY_VERSION,
        )
        if existing_id and self.mode == "enforce":
            existing = self.repo.get_action(existing_id)
            if (
                existing["status"] != "observed"
                or existing["decision"] != prepared.match.outcome_type
                or json.loads(str(existing["before_json"])) != before
            ):
                raise StaleGovernanceAction("observed plan action cannot be promoted")
            self.connection.execute(
                "UPDATE governance_actions SET after_json=?,status='applied',applied_at=? "
                "WHERE id=? AND status='observed'",
                (canonical_snapshot(after), now, existing_id),
            )
            return existing_id
        envelope = DecisionEnvelope(
            domain="plan",
            subject_ref=prepared.match.plan_ids[0],
            input_fingerprint=prepared.fingerprint,
            policy_version=PLAN_FULFILLMENT_POLICY_VERSION,
            tier="deterministic",
            decision=prepared.match.outcome_type,
            confidence=1.0,
            resolution_rule=prepared.match.reason,
            resolver_model=None,
        )
        action = GovernanceActionRepository(self.connection).record(
            envelope,
            before=before,
            after=after,
            status="applied" if self.mode == "enforce" else "observed",
            created_at=now,
            applied_at=now if self.mode == "enforce" else None,
        )
        return str(action["id"])

    def rollback(self, action_id: str, *, now: str, reason: str) -> dict[str, Any]:
        """Rollback only while every changed row still equals the action after snapshot."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            action = self.repo.get_action(action_id)
            after = json.loads(str(action["after_json"]))
            before = json.loads(str(action["before_json"]))
            plan_ids = tuple(str(item) for item in after["plans"])
            outcome_ids = tuple(str(item) for item in after["outcomes"])
            relation_ids = tuple(str(item) for item in after["relations"])
            placeholders = ",".join("?" for _ in plan_ids)
            later = self.connection.execute(
                "SELECT 1 FROM plan_outcomes WHERE status='applied' AND created_at>? "
                f"AND plan_claim_id IN ({placeholders}) LIMIT 1",
                (action["created_at"], *plan_ids),
            ).fetchone()
            if later is not None:
                raise StaleGovernanceAction("a later plan outcome depends on this action")
            current = _action_state(self.connection, plan_ids, outcome_ids, relation_ids)
            GovernanceActionRepository(self.connection).mark_rolled_back(
                action_id, current=current, reason=reason, rolled_back_at=now
            )
            for plan_id, state in before["plans"].items():
                self.connection.execute("UPDATE claims SET valid_to=? WHERE id=?", (state["valid_to"], plan_id))
            if outcome_ids:
                placeholders = ",".join("?" for _ in outcome_ids)
                self.connection.execute(
                    f"UPDATE plan_outcomes SET status='rolled_back' WHERE id IN ({placeholders})",
                    outcome_ids,
                )
            if relation_ids:
                placeholders = ",".join("?" for _ in relation_ids)
                self.connection.execute(
                    f"UPDATE memory_relations SET valid_to=? WHERE id IN ({placeholders}) AND valid_to IS NULL",
                    (now, *relation_ids),
                )
            self.connection.commit()
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise
        return {"status": "rolled_back", "action_id": action_id}


def _claim_input(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: claim.get(key)
        for key in (
            "id",
            "namespace_key",
            "fact_hash",
            "canonical_target_entity_id",
            "qualifiers",
            "valid_from",
            "valid_to",
            "recorded_to",
            "superseded_by_id",
            "status",
        )
    }


def _plan_state(connection: sqlite3.Connection, plan_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "plans": {plan_id: {"valid_to": _valid_to(connection, plan_id)} for plan_id in plan_ids},
        "outcomes": {},
        "relations": {},
    }


def _valid_to(connection: sqlite3.Connection, claim_id: str) -> str | None:
    row = connection.execute("SELECT valid_to FROM claims WHERE id=?", (claim_id,)).fetchone()
    return row["valid_to"] if row else None


def _action_state(
    connection: sqlite3.Connection,
    plan_ids: tuple[str, ...],
    outcome_ids: tuple[str, ...] | list[str],
    relation_ids: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    return {
        "plans": {plan_id: {"valid_to": _valid_to(connection, plan_id)} for plan_id in plan_ids},
        "outcomes": {
            outcome_id: {
                "status": connection.execute("SELECT status FROM plan_outcomes WHERE id=?", (outcome_id,)).fetchone()[
                    "status"
                ]
            }
            for outcome_id in outcome_ids
        },
        "relations": {
            relation_id: {
                "valid_to": connection.execute(
                    "SELECT valid_to FROM memory_relations WHERE id=?", (relation_id,)
                ).fetchone()["valid_to"]
            }
            for relation_id in relation_ids
        },
    }


def _outcome_values(
    prepared: _Prepared,
    plan_id: str,
    status: str,
    relation_id: str | None,
    now: str,
    outcome_type: str | None = None,
) -> dict[str, Any]:
    quantity = prepared.match.quantity
    cumulative = prepared.cumulative
    return {
        "namespace_key": prepared.match.coordinate.namespace,
        "plan_claim_id": plan_id,
        "result_claim_id": prepared.result["id"],
        "outcome_type": outcome_type
        or ("partial" if prepared.match.outcome_type == "ambiguous" else prepared.match.outcome_type),
        "coordinate_hash": coordinate_hash(prepared.match.coordinate),
        "matched_quantity_text": decimal_text(quantity) if quantity is not None else None,
        "unit": prepared.match.coordinate.quantity.unit,
        "cumulative_quantity_text": decimal_text(cumulative) if cumulative is not None else None,
        "match_rule": prepared.match.reason,
        "match_confidence": 1.0,
        "input_fingerprint": prepared.fingerprint,
        "policy_version": PLAN_FULFILLMENT_POLICY_VERSION,
        "status": status,
        "relation_id": relation_id,
        "created_at": now,
        "applied_at": now if status == "applied" else None,
    }
