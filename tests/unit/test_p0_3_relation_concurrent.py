"""P0-3：关系发现应用前端点状态变化回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hl_mem.protocols import RelationProposal
from hl_mem.storage.database import Database
from hl_mem.workers.discover_relations import discover_relations


class EndpointChangingDiscoverer:
    """在模型提案返回前模拟另一流程关闭端点。"""

    def __init__(self, connection: sqlite3.Connection, endpoint_id: str, status: str) -> None:
        self.connection = connection
        self.endpoint_id = endpoint_id
        self.status = status

    def propose(
        self,
        source_claim: dict[str, object],
        candidates: list[dict[str, object]],
        *,
        max_proposals: int,
    ) -> list[RelationProposal]:
        del max_proposals
        target_id = str(candidates[0]["id"])
        self.connection.execute("UPDATE claims SET status=? WHERE id=?", (self.status, self.endpoint_id))
        self.connection.commit()
        return [
            RelationProposal(
                from_claim_id=str(source_claim["id"]),
                to_claim_id=target_id,
                relation="supports",
                confidence=0.99,
                rationale="测试并发状态变化",
                supporting_claim_ids=(),
                model="fake",
            )
        ]


def _insert_claims(connection: sqlite3.Connection) -> None:
    """写入关系发现所需的两个端点。"""
    connection.executemany(
        "INSERT INTO claims("
        "id,namespace_key,predicate,value_json,status,confidence,recorded_from"
        ") VALUES(?,?,?,?,?,?,?)",
        (
            ("source", "default", "p", '"source"', "active", 1.0, "2026-01-01T00:00:00+00:00"),
            ("target", "default", "p", '"target"', "active", 1.0, "2026-01-02T00:00:00+00:00"),
        ),
    )
    connection.commit()


def _run_with_changed_endpoint(tmp_path: Path, endpoint_id: str, status: str) -> tuple[dict[str, int], sqlite3.Row]:
    database = Database(tmp_path / f"relation-{endpoint_id}-{status}.db")
    connection = database.open()
    try:
        _insert_claims(connection)
        result = discover_relations(
            connection,
            EndpointChangingDiscoverer(connection, endpoint_id, status),
            "source",
            mode="auto",
            pool_limit=5,
            max_proposals=1,
            auto_apply_confidence=0.8,
            conflict_confidence=0.9,
        )
        proposal = connection.execute(
            "SELECT status,decision_reason FROM relation_proposals ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert proposal is not None
        return result, proposal
    finally:
        database.close()


def test_archived_source_is_rejected_as_stale_input(tmp_path: Path) -> None:
    """LLM 返回后 source 已归档时不得自动落地关系。"""
    result, proposal = _run_with_changed_endpoint(tmp_path, "source", "archived")
    assert result["applied"] == 0
    assert proposal["status"] == "rejected"
    assert proposal["decision_reason"] == "stale-input"


def test_expired_target_is_rejected_as_stale_input(tmp_path: Path) -> None:
    """LLM 返回后 target 已过期时不得自动落地关系。"""
    result, proposal = _run_with_changed_endpoint(tmp_path, "target", "expired")
    assert result["applied"] == 0
    assert proposal["status"] == "rejected"
    assert proposal["decision_reason"] == "stale-input"
