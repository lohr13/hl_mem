"""SQLite access for plan candidates, outcomes, and their explicit relations."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Iterable, Mapping

from hl_mem.domain.relations import RelationProvenance, add_relation
from hl_mem.storage.claims import ClaimRepository


class StalePlanOutcome(RuntimeError):
    """The idempotency key already represents another input snapshot."""


class PlanFulfillmentRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def find_open_plans(self, result: Mapping[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        target = result.get("canonical_target_entity_id")
        occurred_at = result.get("occurred_start") or result.get("valid_from")
        if not target or not occurred_at or limit < 1:
            return []
        rows = self.connection.execute(
            "SELECT id FROM claims WHERE namespace_key=? AND canonical_target_entity_id=? "
            "AND status='active' AND canonical_attribute LIKE 'plan.%' "
            "AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?) "
            "ORDER BY valid_from,id LIMIT ?",
            (result["namespace_key"], target, occurred_at, occurred_at, limit),
        ).fetchall()
        return list(ClaimRepository(self.connection).batch_get_claims([str(row["id"]) for row in rows]).values())

    def equivalent_pairs(self, claim_ids: Iterable[str]) -> list[tuple[str, str]]:
        ids = sorted(set(claim_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            "SELECT left_claim_id,right_claim_id FROM dedup_pairs WHERE decision='equivalent' "
            f"AND left_claim_id IN ({placeholders}) AND right_claim_id IN ({placeholders})",
            (*ids, *ids),
        ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def list_for_result(self, result_claim_id: str, policy_version: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM plan_outcomes WHERE result_claim_id=? AND policy_version=? ORDER BY created_at,id",
            (result_claim_id, policy_version),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_outcome(self, values: Mapping[str, Any]) -> dict[str, Any]:
        stored = {"id": uuid.uuid4().hex, **values}
        columns = list(stored)
        self.connection.execute(
            f"INSERT OR IGNORE INTO plan_outcomes({','.join(columns)}) " f"VALUES({','.join('?' for _ in columns)})",
            [stored[column] for column in columns],
        )
        row = self.connection.execute(
            "SELECT * FROM plan_outcomes WHERE plan_claim_id=? AND result_claim_id=? "
            "AND outcome_type=? AND policy_version=?",
            (
                values["plan_claim_id"],
                values["result_claim_id"],
                values["outcome_type"],
                values["policy_version"],
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("plan outcome insert was not observable")
        result = dict(row)
        if result["input_fingerprint"] != values["input_fingerprint"]:
            raise StalePlanOutcome("same plan/result/outcome key has another input fingerprint")
        if result["status"] in {"candidate", "observed"} and values["status"] != result["status"]:
            self.connection.execute(
                "UPDATE plan_outcomes SET status=?,cumulative_quantity_text=?,relation_id=?,applied_at=? " "WHERE id=?",
                (
                    values["status"],
                    values.get("cumulative_quantity_text"),
                    values.get("relation_id"),
                    values.get("applied_at"),
                    result["id"],
                ),
            )
            result = dict(self.connection.execute("SELECT * FROM plan_outcomes WHERE id=?", (result["id"],)).fetchone())
        return result

    def applied_partials(self, plan_claim_id: str, policy_version: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT po.*,c.valid_from AS result_valid_from FROM plan_outcomes AS po "
            "JOIN claims AS c ON c.id=po.result_claim_id "
            "WHERE po.plan_claim_id=? AND po.policy_version=? AND po.outcome_type='partial' "
            "AND po.status='applied' ORDER BY c.valid_from,po.result_claim_id",
            (plan_claim_id, policy_version),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_relation(self, plan_id: str, result_id: str, relation: str, now: str) -> str:
        existing = self.connection.execute(
            "SELECT id FROM memory_relations WHERE from_id=? AND to_id=? AND relation=? AND valid_to IS NULL",
            (plan_id, result_id, relation),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        return add_relation(
            self.connection,
            plan_id,
            result_id,
            relation,
            provenance=RelationProvenance.DETERMINISTIC,
            created_at=now,
        )

    def action_id(self, plan_id: str, input_fingerprint: str, policy_version: str) -> str | None:
        row = self.connection.execute(
            "SELECT id FROM governance_actions WHERE domain='plan' AND subject_ref=? "
            "AND input_fingerprint=? AND policy_version=?",
            (plan_id, input_fingerprint, policy_version),
        ).fetchone()
        return str(row["id"]) if row else None

    def get_action(self, action_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM governance_actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise KeyError(action_id)
        return dict(row)
