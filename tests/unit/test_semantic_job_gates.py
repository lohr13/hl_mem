from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.workers import job_handlers
from hl_mem.workers.consolidate import enqueue_daily_consolidation
from hl_mem.workers.deduplicate import enqueue_daily_deduplication
from hl_mem.workers.induce_policies import enqueue_daily_policy_induction
from hl_mem.workers.maintenance import enqueue_daily_reclassify
from hl_mem.workers.worker import Worker

NOW = "2026-08-31T00:00:00+00:00"


def test_disabled_semantic_schedulers_do_not_insert_jobs(tmp_path: Path) -> None:
    database = Database(tmp_path / "queue-gates.db")
    connection = database.open()

    assert enqueue_daily_consolidation(connection, NOW, "00:00", enabled=False) is False
    assert enqueue_daily_deduplication(connection, NOW, 0, enabled=False) is False
    assert enqueue_daily_policy_induction(connection, NOW, "00:00", enabled=False) is False
    assert enqueue_daily_reclassify(connection, NOW, "00:00", enabled=False) is False
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    database.close()


@pytest.mark.parametrize(
    "job_type",
    (
        "consolidate_conflicts",
        "deduplicate_claims",
        "discover_relations",
        "induce_policies",
        "reclassify_claims",
    ),
)
def test_disabled_semantic_job_stops_before_handler_resolution(tmp_path: Path, job_type: str) -> None:
    database = Database(tmp_path / f"handler-disabled-{job_type}.db")
    connection = database.open()
    worker = Worker(Settings.for_test(), connection=connection)

    result = job_handlers.dispatch_job(worker, {"job_type": job_type, "payload_json": "{}"})

    assert result == {
        "status": "disabled",
        "reason": "disabled_by_configuration",
        "job_type": job_type,
    }
    worker.close()
    database.close()


@pytest.mark.parametrize(
    ("job_type", "field_name", "enabled_value"),
    (
        ("consolidate_conflicts", "semantic_conflict_consolidation_enabled", True),
        ("deduplicate_claims", "dedup_llm_enabled", True),
        ("discover_relations", "relation_discovery_mode", "audit"),
        ("induce_policies", "policy_induction_enabled", True),
        ("reclassify_claims", "reclassify_enabled", True),
    ),
)
def test_enabling_one_semantic_job_reaches_only_its_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
    field_name: str,
    enabled_value: bool | str,
) -> None:
    database = Database(tmp_path / f"handler-enabled-{job_type}.db")
    connection = database.open()
    settings = replace(Settings.for_test(), **{field_name: enabled_value})
    worker = Worker(settings, connection=connection)
    calls: list[str] = []

    def handler(_worker: Worker, job: dict[str, object]) -> dict[str, str]:
        calls.append(str(job["job_type"]))
        return {"status": "executed"}

    monkeypatch.setitem(job_handlers.JOB_HANDLERS, job_type, handler)

    assert job_handlers.dispatch_job(worker, {"job_type": job_type, "payload_json": "{}"}) == {"status": "executed"}
    assert calls == [job_type]
    worker.close()
    database.close()
