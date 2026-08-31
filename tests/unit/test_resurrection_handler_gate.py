from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.storage.deferred_tasks import DeferredTaskRepository
from hl_mem.workers.deferred import process_deferred_tasks, process_recall_side_effect_tasks

NOW = "2026-08-31T00:00:00+00:00"
DISABLED = frozenset({"resurrect_recalled_claim"})


def _defer_resurrection(connection: object, suffix: str) -> None:
    DeferredTaskRepository(connection).defer(  # type: ignore[arg-type]
        task_type="resurrect_recalled_claim",
        resource_type="claim",
        resource_id=f"claim-{suffix}",
        payload={},
        idempotency_key=f"resurrection-{suffix}",
        run_after=NOW,
        max_attempts=3,
        error="queued before upgrade",
        updated_at=NOW,
    )


def test_general_deferred_loop_abandons_disabled_resurrection_before_handler(tmp_path: Path) -> None:
    database = Database(tmp_path / "general-resurrection-gate.db")
    connection = database.open()
    _defer_resurrection(connection, "general")

    result = process_deferred_tasks(connection, now=NOW, disabled_task_types=DISABLED)
    task = DeferredTaskRepository(connection).get_by_idempotency_key("resurrection-general")

    assert result["abandoned"] == 1
    assert task is not None
    assert task["status"] == "abandoned"
    assert task["last_error"] == "disabled_by_configuration"
    database.close()


def test_recall_side_effect_loop_abandons_disabled_resurrection_before_handler(tmp_path: Path) -> None:
    database = Database(tmp_path / "side-effect-resurrection-gate.db")
    connection = database.open()
    _defer_resurrection(connection, "side-effect")

    result = process_recall_side_effect_tasks(connection, now=NOW, disabled_task_types=DISABLED)
    task = DeferredTaskRepository(connection).get_by_idempotency_key("resurrection-side-effect")

    assert result["abandoned"] == 1
    assert task is not None
    assert task["status"] == "abandoned"
    assert task["last_error"] == "disabled_by_configuration"
    database.close()
