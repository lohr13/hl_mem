"""P1-3：关系提案运行身份与不可变审计记录测试。"""

from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.storage.relation_proposals import RelationProposalRepository


def test_audit_and_auto_runs_create_independent_proposals(tmp_path: Path) -> None:
    """不同 rollout 模式的运行不得复用并覆盖旧 proposal。"""
    database = Database(tmp_path / "proposal-runs.db")
    connection = database.open()
    try:
        connection.executemany(
            "INSERT INTO claims(id,status,recorded_from) VALUES(?,?,?)",
            (
                ("source", "active", "2026-01-01T00:00:00+00:00"),
                ("target", "active", "2026-01-01T00:00:00+00:00"),
            ),
        )
        repository = RelationProposalRepository(connection)
        base = {
            "source_claim_id": "source",
            "target_claim_id": "target",
            "relation": "supports",
            "confidence": 0.9,
            "rationale": "first",
            "supporting_claim_ids": (),
            "model": "model-a",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        }

        audit_id = repository.insert_proposal({**base, "run_id": "run-audit", "mode": "audit"})
        auto_id = repository.insert_proposal(
            {
                **base,
                "run_id": "run-auto",
                "mode": "auto",
                "model": "model-b",
                "rationale": "second",
            }
        )

        assert audit_id is not None and auto_id is not None and audit_id != auto_id
        rows = connection.execute(
            "SELECT run_id,mode,model,rationale FROM relation_proposals ORDER BY mode"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("run-audit", "audit", "model-a", "first"),
            ("run-auto", "auto", "model-b", "second"),
        ]
    finally:
        database.close()


def test_same_mode_different_runs_are_both_retained(tmp_path: Path) -> None:
    """同模式的不同运行必须各自保留不可变 proposal。"""
    database = Database(tmp_path / "proposal-dedup.db")
    connection = database.open()
    try:
        connection.executemany(
            "INSERT INTO claims(id,status,recorded_from) VALUES(?,?,?)",
            (
                ("source", "active", "2026-01-01T00:00:00+00:00"),
                ("target", "active", "2026-01-01T00:00:00+00:00"),
            ),
        )
        repository = RelationProposalRepository(connection)
        proposal = {
            "source_claim_id": "source",
            "target_claim_id": "target",
            "relation": "supports",
            "confidence": 0.9,
            "rationale": "audit",
            "supporting_claim_ids": (),
            "model": "model",
            "mode": "audit",
            "status": "pending",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        first_id = repository.insert_proposal({**proposal, "run_id": "run-1"})
        second_id = repository.insert_proposal({**proposal, "run_id": "run-2"})
        assert first_id is not None and second_id is not None and first_id != second_id
        rows = connection.execute(
            "SELECT run_id,mode FROM relation_proposals ORDER BY run_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("run-1", "audit"), ("run-2", "audit")]
        assert repository.insert_proposal({**proposal, "run_id": "run-2"}) is None
    finally:
        database.close()
