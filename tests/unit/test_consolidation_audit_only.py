from __future__ import annotations

from pathlib import Path

import pytest

from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.consolidate import ConflictConsolidator, ConsolidationDecision


class FixedJudge:
    def __init__(self, decision: ConsolidationDecision) -> None:
        self.decision = decision

    def judge(self, _left: dict[str, object], _right: dict[str, object]) -> ConsolidationDecision:
        return self.decision


def _claim(connection: object, claim_id: str, vector: list[float]) -> None:
    repository = ClaimRepository(connection)  # type: ignore[arg-type]
    assert repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "subject_entity_id": "user",
            "canonical_attribute": "choice.tool",
            "canonical_slot": "choice.tool",
            "qualifiers": {},
            "predicate": "uses",
            "value": claim_id,
            "status": "active",
            "scope": "permanent",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "embedding_dense": pack_vector(vector),
            "embedding_model": "fake-v1",
        }
    )


def _memory_snapshot(connection: object) -> dict[str, list[tuple[object, ...]]]:
    queries = {
        "claims": "SELECT id,status,superseded_by_id,valid_to,recorded_to FROM claims ORDER BY id",
        "relations": "SELECT * FROM memory_relations ORDER BY id",
        "conflicts": "SELECT * FROM conflict_cases ORDER BY id",
        "evidence": "SELECT * FROM evidence_links ORDER BY id",
    }
    return {
        name: [tuple(row) for row in connection.execute(query)]  # type: ignore[attr-defined]
        for name, query in queries.items()
    }


@pytest.mark.parametrize(
    ("kind", "current_claim_id"),
    (("contradiction", None), ("state_change", "right")),
)
def test_high_confidence_semantic_decisions_are_audit_only(
    tmp_path: Path,
    kind: str,
    current_claim_id: str | None,
) -> None:
    database = Database(tmp_path / f"audit-{kind}.db")
    connection = database.open()
    _claim(connection, "left", [1.0, 0.0])
    _claim(connection, "right", [0.8, 0.6])
    before = _memory_snapshot(connection)
    consolidator = ConflictConsolidator(
        connection,
        FixedJudge(ConsolidationDecision(kind, 1.0, "model judgment", current_claim_id)),
    )

    result = consolidator.run_batch(10)

    assert result["reviewed"] == 1
    assert result[kind] == 1
    assert _memory_snapshot(connection) == before
    audit = connection.execute("SELECT decision,confidence,rationale FROM consolidation_pairs").fetchone()
    assert tuple(audit) == (f"audit_only:{kind}", 1.0, "model judgment")
    database.close()


def test_semantic_dry_run_does_not_write_audit_or_memory(tmp_path: Path) -> None:
    database = Database(tmp_path / "audit-dry-run.db")
    connection = database.open()
    _claim(connection, "left", [1.0, 0.0])
    _claim(connection, "right", [0.8, 0.6])
    before = _memory_snapshot(connection)

    result = ConflictConsolidator(
        connection,
        FixedJudge(ConsolidationDecision("contradiction", 1.0, "dry run")),
    ).run_batch(10, dry_run=True)

    assert result["contradiction"] == 1
    assert _memory_snapshot(connection) == before
    assert connection.execute("SELECT count(*) FROM consolidation_pairs").fetchone()[0] == 0
    database.close()
