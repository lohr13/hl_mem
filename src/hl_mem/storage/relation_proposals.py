"""关系候选审计仓储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from hl_mem.domain.relations import RelationProvenance, _insert_relation
from hl_mem.storage._shared import insert_row


class RelationProposalRepository:
    """关系候选审计记录的持久化仓储。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_proposal(self, proposal: dict[str, Any], commit: bool = True) -> str | None:
        """插入本次运行的不可变提案；仅同一 run 的唯一键冲突返回 None。"""
        stored = dict(proposal)
        stored.setdefault("id", uuid.uuid4().hex)
        stored.setdefault("run_id", uuid.uuid4().hex)
        stored["supporting_claim_ids_json"] = json.dumps(
            stored.pop("supporting_claim_ids", []),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        inserted = insert_row(self.connection, "relation_proposals", stored, commit)
        return str(stored["id"]) if inserted else None

    def update_proposal_status(
        self,
        proposal_id: str,
        status: str,
        *,
        decision_reason: str | None = None,
        relation_id: str | None = None,
        conflict_case_id: str | None = None,
        decided_at: str | None = None,
        commit: bool = True,
    ) -> bool:
        """更新提案决策状态及其落地对象。"""
        if status == "applied":
            raise ValueError("use approve_proposal to apply a relation proposal")
        cursor = self.connection.execute(
            "UPDATE relation_proposals SET status=?,decision_reason=?,relation_id=?,conflict_case_id=?,decided_at=? "
            "WHERE id=?",
            (
                status,
                decision_reason,
                relation_id,
                conflict_case_id,
                decided_at,
                proposal_id,
            ),
        )
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

    def approve_proposal(self, proposal_id: str, *, decided_at: str) -> str:
        """Atomically materialize one pending proposal as an official relation."""
        existing = self.connection.execute(
            "SELECT * FROM relation_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if existing is None:
            raise KeyError(proposal_id)
        if existing["status"] == "applied" and existing["relation_id"]:
            return str(existing["relation_id"])
        if existing["status"] != "pending":
            raise ValueError(f"relation proposal is not pending: {existing['status']}")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            proposal = self.connection.execute(
                "SELECT * FROM relation_proposals WHERE id=? AND status='pending'",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ValueError("relation proposal changed before approval")
            relation_id = _insert_relation(
                self.connection,
                str(proposal["source_claim_id"]),
                str(proposal["target_claim_id"]),
                str(proposal["relation"]),
                float(proposal["confidence"]),
                provenance=RelationProvenance.APPROVED_PROPOSAL,
                evidence_ids=tuple(json.loads(str(proposal["supporting_claim_ids_json"]))),
                proposal_id=proposal_id,
                created_at=decided_at,
            )
            cursor = self.connection.execute(
                "UPDATE relation_proposals SET status='applied',decision_reason='approved',relation_id=?,"
                "decided_at=? WHERE id=? AND status='pending'",
                (relation_id, decided_at, proposal_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("relation proposal approval lost")
            self.connection.commit()
            return relation_id
        except Exception:
            self.connection.rollback()
            raise

    def get_pending_proposals(self, limit: int = 100) -> list[dict[str, Any]]:
        """按确定性顺序返回待决提案。"""
        rows = self.connection.execute(
            "SELECT * FROM relation_proposals WHERE status='pending' ORDER BY created_at,id LIMIT ?",
            (limit,),
        ).fetchall()
        result = [dict(row) for row in rows]
        for proposal in result:
            proposal["supporting_claim_ids"] = json.loads(proposal.pop("supporting_claim_ids_json"))
        return result
