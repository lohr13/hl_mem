from __future__ import annotations

import json

from hl_mem.application.deletion import DeletionService
from hl_mem.observability.audit import audit_scope
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database

NOW = "2026-08-28T00:00:00+00:00"


def _insert_claim(connection, claim_id: str = "claim") -> None:
    assert ClaimRepository(connection).insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "recorded_from": NOW,
            "status": "active",
            "subject_entity_id": "hl_mem",
            "predicate": "配置",
            "value": "0.32.0",
            "canonical_attribute": "config.version",
            "importance": 0.5,
        }
    )


def test_claim_update_and_delete_are_audited_at_database_boundary(tmp_path) -> None:
    connection = Database(tmp_path / "claim-mutation.db").open()
    _insert_claim(connection)

    with audit_scope(
        trace_id="trace-reclassify",
        job_id="job-reclassify",
        related_claim_id="related-claim",
        claim_mutation_source="reclassify_claims",
    ):
        connection.execute(
            "UPDATE claims SET canonical_slot='config.version',importance=0.7 WHERE id='claim'"
        )
    connection.execute("DELETE FROM claims WHERE id='claim'")

    rows = connection.execute(
        "SELECT phase,action,outcome,trace_id,claim_id,related_claim_id,job_id,detail_json FROM audit_log "
        "WHERE phase='claim_mutation' ORDER BY id"
    ).fetchall()
    assert [(row["phase"], row["action"], row["outcome"], row["claim_id"]) for row in rows] == [
        ("claim_mutation", "updated", "applied", "claim"),
        ("claim_mutation", "deleted", "applied", "claim"),
    ]
    update_detail, delete_detail = (json.loads(row["detail_json"]) for row in rows)
    assert rows[0]["trace_id"] == "trace-reclassify"
    assert rows[0]["job_id"] == "job-reclassify"
    assert rows[0]["related_claim_id"] == "related-claim"
    assert rows[1]["job_id"] is None
    assert update_detail == {
        "schema_version": "claim_mutation_audit_v1",
        "operation": "update",
        "source": "reclassify_claims",
        "changed_fields": ["importance", "canonical_slot"],
        "old_status": "active",
        "new_status": "active",
        "old_canonical_slot": None,
        "new_canonical_slot": "config.version",
        "old_importance": 0.5,
        "new_importance": 0.7,
    }
    assert delete_detail == {
        "schema_version": "claim_mutation_audit_v1",
        "operation": "delete",
        "source": "database",
        "old_status": "active",
        "old_canonical_slot": "config.version",
        "old_importance": 0.7,
    }


def test_claim_mutation_audit_rolls_back_with_the_claim_change(tmp_path) -> None:
    connection = Database(tmp_path / "claim-mutation-rollback.db").open()
    _insert_claim(connection)

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE claims SET status='archived' WHERE id='claim'")
    assert connection.execute(
        "SELECT count(*) FROM audit_log WHERE phase='claim_mutation' AND claim_id='claim'"
    ).fetchone()[0] == 1
    connection.rollback()

    assert connection.execute("SELECT status FROM claims WHERE id='claim'").fetchone()[0] == "active"
    assert connection.execute(
        "SELECT count(*) FROM audit_log WHERE phase='claim_mutation' AND claim_id='claim'"
    ).fetchone()[0] == 0


def test_physical_deletion_audit_identifies_the_entry_path(tmp_path) -> None:
    connection = Database(tmp_path / "physical-deletion.db").open()
    _insert_claim(connection)

    result = DeletionService(
        connection,
        ledger_path=tmp_path / "physical-deletion.tombstones.jsonl",
    ).delete_claim("claim")

    row = connection.execute(
        "SELECT detail_json FROM audit_log "
        "WHERE phase='claim_mutation' AND action='deleted' AND claim_id='claim'"
    ).fetchone()
    assert result.deleted is True
    assert row is not None
    assert json.loads(row["detail_json"])["source"] == "delete_claim"
