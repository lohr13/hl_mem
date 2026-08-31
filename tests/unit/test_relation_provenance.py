from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.relation_proposals import RelationProposalRepository

NOW = "2026-08-31T00:00:00+00:00"


def _claims(connection: sqlite3.Connection) -> None:
    repository = ClaimRepository(connection)
    for claim_id in ("source", "target"):
        assert repository.insert_claim(
            {
                "id": claim_id,
                "namespace_key": "default",
                "predicate": "knows",
                "value": claim_id,
                "recorded_from": NOW,
                "status": "active",
            }
        )


def _proposal(repository: RelationProposalRepository, suffix: str) -> str:
    proposal_id = repository.insert_proposal(
        {
            "id": f"proposal-{suffix}",
            "run_id": f"run-{suffix}",
            "source_claim_id": "source",
            "target_claim_id": "target",
            "relation": "supports",
            "confidence": 0.9,
            "rationale": "supported by evidence",
            "supporting_claim_ids": (),
            "model": "test-model",
            "mode": "audit",
            "status": "pending",
            "created_at": NOW,
        }
    )
    assert proposal_id is not None
    return proposal_id


def test_new_official_relation_requires_explicit_nonlegacy_provenance(tmp_path: Path) -> None:
    relations = importlib.import_module("hl_mem.domain.relations")
    database = Database(tmp_path / "official-provenance.db")
    connection = database.open()
    _claims(connection)

    with pytest.raises(TypeError, match="provenance"):
        relations.add_relation(connection, "source", "target", "supports")
    with pytest.raises(ValueError, match="legacy provenance"):
        relations.add_relation(
            connection,
            "source",
            "target",
            "supports",
            provenance=relations.RelationProvenance.LEGACY,
        )
    relation_id = relations.add_relation(
        connection,
        "source",
        "target",
        "supports",
        provenance=relations.RelationProvenance.MANUAL,
        created_at=NOW,
    )

    assert tuple(
        connection.execute(
            "SELECT provenance,proposal_id,evidence_json FROM memory_relations WHERE id=?",
            (relation_id,),
        ).fetchone()
    ) == ("manual", None, "[]")
    database.close()


def test_approving_proposal_atomically_creates_provenanced_relation(tmp_path: Path) -> None:
    database = Database(tmp_path / "approve-proposal.db")
    connection = database.open()
    _claims(connection)
    repository = RelationProposalRepository(connection)
    proposal_id = _proposal(repository, "approve")

    with pytest.raises(ValueError, match="approve_proposal"):
        repository.update_proposal_status(proposal_id, "applied")

    relation_id = repository.approve_proposal(proposal_id, decided_at=NOW)

    assert repository.approve_proposal(proposal_id, decided_at=NOW) == relation_id
    assert tuple(
        connection.execute(
            "SELECT from_id,to_id,relation,provenance,proposal_id FROM memory_relations WHERE id=?",
            (relation_id,),
        ).fetchone()
    ) == ("source", "target", "supports", "approved_proposal", proposal_id)
    assert tuple(
        connection.execute(
            "SELECT status,relation_id,decided_at FROM relation_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
    ) == ("applied", relation_id, NOW)
    database.close()


def test_proposal_approval_rolls_back_relation_if_status_update_fails(tmp_path: Path) -> None:
    database = Database(tmp_path / "approve-rollback.db")
    connection = database.open()
    _claims(connection)
    repository = RelationProposalRepository(connection)
    proposal_id = _proposal(repository, "rollback")
    connection.execute(
        "CREATE TRIGGER reject_proposal_apply BEFORE UPDATE OF status ON relation_proposals "
        "WHEN NEW.status='applied' BEGIN SELECT RAISE(ABORT,'reject apply'); END"
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="reject apply"):
        repository.approve_proposal(proposal_id, decided_at=NOW)

    assert (
        connection.execute("SELECT count(*) FROM memory_relations WHERE proposal_id=?", (proposal_id,)).fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT status FROM relation_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
        == "pending"
    )
    database.close()
