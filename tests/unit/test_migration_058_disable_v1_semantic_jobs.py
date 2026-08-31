from __future__ import annotations

import json
from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.storage.deferred_tasks import DeferredTaskRepository

MIGRATION = "058_disable_v1_semantic_jobs"
NOW = "2026-08-31T00:00:00+00:00"
SEMANTIC_JOB_TYPES = (
    "consolidate_conflicts",
    "deduplicate_claims",
    "discover_relations",
    "induce_policies",
    "reclassify_claims",
)


def _job(connection: object, job_id: str, job_type: str, status: str) -> None:
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO jobs(id,job_type,payload_json,status,leased_until,lease_token,last_error,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            job_type,
            json.dumps({"seed": job_id}),
            status,
            "2026-08-31T01:00:00+00:00",
            f"lease-{job_id}",
            f"error-{job_id}",
            NOW,
            NOW,
        ),
    )


def _seed_pre_058(path: Path) -> dict[str, tuple[object, ...]]:
    database = Database(path)
    connection = database.open()
    for job_type in SEMANTIC_JOB_TYPES:
        _job(connection, f"{job_type}-pending", job_type, "pending")
    for status in ("running", "succeeded", "failed", "dead"):
        _job(connection, f"consolidate-{status}", "consolidate_conflicts", status)
    _job(connection, "extract-pending", "extract_event", "pending")
    tasks = DeferredTaskRepository(connection)
    tasks.defer(
        task_type="resurrect_recalled_claim",
        resource_type="claim",
        resource_id="archived-claim",
        payload={"claim_id": "archived-claim"},
        idempotency_key="resurrection-before-v1",
        run_after=NOW,
        max_attempts=3,
        error="queued before v1",
        updated_at=NOW,
    )
    tasks.defer(
        task_type="record_recall_access",
        resource_type="claim",
        resource_id="active-claim",
        payload={"claim_id": "active-claim"},
        idempotency_key="access-before-v1",
        run_after=NOW,
        max_attempts=3,
        error="queued before v1",
        updated_at=NOW,
    )
    preserved = {
        row["id"]: tuple(row)
        for row in connection.execute(
            "SELECT id,status,leased_until,lease_token,last_error,updated_at FROM jobs "
            "WHERE id IN ('consolidate-running','consolidate-succeeded','consolidate-failed',"
            "'consolidate-dead','extract-pending') ORDER BY id"
        )
    }
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (MIGRATION,))
    connection.commit()
    database.close()
    return preserved


def _governance_snapshot(connection: object) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    jobs = [
        tuple(row)
        for row in connection.execute(  # type: ignore[attr-defined]
            "SELECT id,status,leased_until,lease_token,last_error FROM jobs ORDER BY id"
        )
    ]
    tasks = [
        tuple(row)
        for row in connection.execute(  # type: ignore[attr-defined]
            "SELECT idempotency_key,status,last_error FROM deferred_tasks ORDER BY idempotency_key"
        )
    ]
    return jobs, tasks


def test_058_disables_only_pending_semantic_and_resurrection_work(tmp_path: Path) -> None:
    path = tmp_path / "disable-semantic-jobs.db"
    preserved = _seed_pre_058(path)

    database = Database(path)
    connection = database.open()

    disabled = {
        row["job_type"]: tuple(row)[1:]
        for row in connection.execute(
            "SELECT job_type,status,leased_until,lease_token,last_error FROM jobs "
            "WHERE id LIKE '%-pending' AND job_type IN ("
            "'consolidate_conflicts','deduplicate_claims','discover_relations','induce_policies','reclassify_claims')"
        )
    }
    assert disabled == {job_type: ("dead", None, None, "disabled_by_v1_migration") for job_type in SEMANTIC_JOB_TYPES}
    preserved_after = {
        row["id"]: tuple(row)
        for row in connection.execute(
            "SELECT id,status,leased_until,lease_token,last_error,updated_at FROM jobs "
            "WHERE id IN ('consolidate-running','consolidate-succeeded','consolidate-failed',"
            "'consolidate-dead','extract-pending') ORDER BY id"
        )
    }
    assert preserved_after == preserved
    resurrection = DeferredTaskRepository(connection).get_by_idempotency_key("resurrection-before-v1")
    access = DeferredTaskRepository(connection).get_by_idempotency_key("access-before-v1")
    assert resurrection is not None
    assert (resurrection["status"], resurrection["last_error"]) == (
        "abandoned",
        "disabled_by_v1_migration",
    )
    assert access is not None
    assert (access["status"], access["last_error"]) == ("pending", "queued before v1")
    assert connection.execute("SELECT count(*) FROM schema_migrations WHERE version=?", (MIGRATION,)).fetchone()[0] == 1

    first = _governance_snapshot(connection)
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (MIGRATION,))
    connection.commit()
    database.close()

    reopened_database = Database(path)
    reopened = reopened_database.open()
    assert _governance_snapshot(reopened) == first
    reopened_database.close()
