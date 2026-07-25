"""关系候选审计仓储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from hl_mem.storage._shared import insert_row


class RelationProposalRepository:
    """关系候选审计记录的持久化仓储。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert_proposal(self, proposal: dict[str, Any], commit: bool = True) -> str | None:
        """幂等插入提案，重复时返回已有提案标识。"""
        stored = dict(proposal)
        stored.setdefault("id", uuid.uuid4().hex)
        stored["supporting_claim_ids_json"] = json.dumps(
            stored.pop("supporting_claim_ids", []), ensure_ascii=False, separators=(",", ":")
        )
        inserted = insert_row(self.connection, "relation_proposals", stored, commit)
        if inserted:
            return str(stored["id"])
        row = self.connection.execute(
            "SELECT id FROM relation_proposals WHERE source_claim_id=? AND target_claim_id=? AND relation=?",
            (stored["source_claim_id"], stored["target_claim_id"], stored["relation"]),
        ).fetchone()
        return str(row["id"]) if row else None

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
        cursor = self.connection.execute(
            "UPDATE relation_proposals SET status=?,decision_reason=?,relation_id=?,conflict_case_id=?,decided_at=? "
            "WHERE id=?",
            (status, decision_reason, relation_id, conflict_case_id, decided_at, proposal_id),
        )
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

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
