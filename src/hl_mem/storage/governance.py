"""治理动作账本的 SQLite 存取与 rollback CAS。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Literal, Mapping, cast

from hl_mem.domain.governance import (
    CONFLICT_AUTO_POLICY_VERSION,
    DecisionEnvelope,
    canonical_snapshot,
    snapshot_fingerprint,
)

GovernanceStatus = Literal["observed", "applied", "rolled_back", "failed"]


class StaleGovernanceAction(RuntimeError):
    """相同输入已有不同决策，或 rollback 的 after 状态已陈旧。"""


class GovernanceActionRepository:
    """只写有界结构快照；事务边界由应用服务持有。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["evidence_ids"] = json.loads(str(result.pop("evidence_ids_json")))
        result["before"] = json.loads(str(result.pop("before_json")))
        result["after"] = json.loads(str(result.pop("after_json")))
        return result

    def record(
        self,
        envelope: DecisionEnvelope,
        *,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        status: GovernanceStatus,
        created_at: str,
        applied_at: str | None = None,
    ) -> dict[str, Any]:
        """以领域/对象/输入/策略为唯一键幂等记录动作。"""

        before_json = canonical_snapshot(before)
        after_json = canonical_snapshot(after)
        evidence_json = json.dumps(sorted(set(envelope.evidence_ids)), ensure_ascii=False, separators=(",", ":"))
        action_id = uuid.uuid4().hex
        self.connection.execute(
            "INSERT OR IGNORE INTO governance_actions("
            "id,domain,subject_ref,input_fingerprint,policy_version,tier,decision,confidence,"
            "resolution_rule,resolver_model,evidence_ids_json,before_json,after_json,status,"
            "created_at,applied_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                action_id,
                envelope.domain,
                envelope.subject_ref,
                envelope.input_fingerprint,
                envelope.policy_version,
                envelope.tier,
                envelope.decision,
                envelope.confidence,
                envelope.resolution_rule,
                envelope.resolver_model,
                evidence_json,
                before_json,
                after_json,
                status,
                created_at,
                applied_at,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM governance_actions WHERE domain=? AND subject_ref=? "
            "AND input_fingerprint=? AND policy_version=?",
            (
                envelope.domain,
                envelope.subject_ref,
                envelope.input_fingerprint,
                envelope.policy_version,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("governance action insert was not observable")
        decoded = self._decode(row)
        stored_hash = snapshot_fingerprint(
            {
                "confidence": decoded["confidence"],
                "decision": decoded["decision"],
                "evidence_ids": decoded["evidence_ids"],
                "resolution_rule": decoded["resolution_rule"],
                "resolver_model": decoded["resolver_model"],
                "tier": decoded["tier"],
            }
        )
        if (
            stored_hash != envelope.decision_hash
            or decoded["before"] != json.loads(before_json)
            or decoded["after"] != json.loads(after_json)
        ):
            raise StaleGovernanceAction("same governance input already has a different decision or snapshot")
        return decoded

    def mark_rolled_back(
        self,
        action_id: str,
        *,
        current: Mapping[str, Any],
        reason: str,
        rolled_back_at: str,
    ) -> dict[str, Any]:
        """仅当调用方读到的当前状态仍等于 after 快照时关闭动作。"""

        row = self.connection.execute(
            "SELECT status,before_json,after_json FROM governance_actions WHERE id=?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise KeyError(action_id)
        if row["status"] != "applied":
            raise StaleGovernanceAction(f"governance action is not applied: {action_id}")
        if snapshot_fingerprint(current) != snapshot_fingerprint(str(row["after_json"])):
            raise StaleGovernanceAction("current state no longer matches governance after snapshot")
        cursor = self.connection.execute(
            "UPDATE governance_actions SET status='rolled_back',rolled_back_at=?,rollback_reason=? "
            "WHERE id=? AND status='applied'",
            (rolled_back_at, reason[:512], action_id),
        )
        if cursor.rowcount != 1:
            raise StaleGovernanceAction(f"governance action changed during rollback: {action_id}")
        return cast(dict[str, Any], json.loads(str(row["before_json"])))


def upgrade_conflict_auto_policy(
    connection: sqlite3.Connection,
    now: str,
    policy_version: str = CONFLICT_AUTO_POLICY_VERSION,
) -> int:
    """把尚未经过当前策略的 open case 一次性重新置 dirty。"""

    connection.execute("BEGIN IMMEDIATE")
    try:
        inserted = connection.execute(
            "INSERT OR IGNORE INTO conflict_review_state("
            "case_id,dirty_at,dirty_reason,policy_version) "
            "SELECT id,?,'v030_policy_upgrade',? FROM conflict_cases "
            "WHERE status IN ('pending','auto_resolved','manual_required') AND resolved_at IS NULL",
            (now, policy_version),
        ).rowcount
        updated = connection.execute(
            "UPDATE conflict_review_state SET dirty_at=?,dirty_reason='v030_policy_upgrade',"
            "not_before=NULL,attempt_count=0,last_error=NULL,policy_version=? "
            "WHERE COALESCE(policy_version,'')<>? AND case_id IN ("
            "SELECT id FROM conflict_cases WHERE status IN ('pending','auto_resolved','manual_required') "
            "AND resolved_at IS NULL)",
            (now, policy_version, policy_version),
        ).rowcount
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return int(inserted) + int(updated)
