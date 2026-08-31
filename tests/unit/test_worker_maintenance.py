from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from hl_mem.observability.audit import NullAuditLogger
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

NOW = "2026-08-31T00:00:00+00:00"


def _operation_names(operations: object) -> list[str]:
    return [operation.name for operation in operations]  # type: ignore[attr-defined,union-attr]


def test_default_deterministic_maintenance_contains_no_semantic_jobs(tmp_path: Path) -> None:
    maintenance = importlib.import_module("hl_mem.workers.maintenance")
    database = Database(tmp_path / "maintenance-defaults.db")
    connection = database.open()

    names = _operation_names(
        maintenance.build_deterministic_maintenance(
            connection,
            Settings.for_test(),
            now=NOW,
            audit=NullAuditLogger(),
        )
    )

    assert "review_pending_near_duplicates" in names
    assert "scan_derived_memories" in names
    assert not any(name.startswith("enqueue_daily_") for name in names)
    database.close()


def test_near_copy_review_has_an_independent_deterministic_switch(tmp_path: Path) -> None:
    maintenance = importlib.import_module("hl_mem.workers.maintenance")
    database = Database(tmp_path / "maintenance-near-copy.db")
    connection = database.open()

    names = _operation_names(
        maintenance.build_deterministic_maintenance(
            connection,
            replace(Settings.for_test(), dedup_enabled=False),
            now=NOW,
            audit=NullAuditLogger(),
        )
    )

    assert "review_pending_near_duplicates" not in names
    database.close()


@pytest.mark.parametrize(
    ("field_name", "operation_name"),
    (
        ("semantic_conflict_consolidation_enabled", "enqueue_daily_consolidation"),
        ("dedup_llm_enabled", "enqueue_daily_deduplication"),
        ("policy_induction_enabled", "enqueue_daily_policy_induction"),
        ("reclassify_enabled", "enqueue_daily_reclassify"),
    ),
)
def test_semantic_schedules_are_independently_opt_in(
    tmp_path: Path,
    field_name: str,
    operation_name: str,
) -> None:
    maintenance = importlib.import_module("hl_mem.workers.maintenance")
    database = Database(tmp_path / f"semantic-{field_name}.db")
    connection = database.open()
    base = replace(Settings.for_test(), llm_api_key="configured")

    default_names = _operation_names(maintenance.build_semantic_schedules(connection, base, now=lambda: NOW))
    enabled_names = _operation_names(
        maintenance.build_semantic_schedules(
            connection,
            replace(base, **{field_name: True}),
            now=lambda: NOW,
        )
    )

    assert default_names == []
    assert enabled_names == [operation_name]
    database.close()


@pytest.mark.parametrize(
    "field_name",
    (
        "semantic_conflict_consolidation_enabled",
        "dedup_llm_enabled",
        "reclassify_enabled",
    ),
)
def test_model_schedules_require_a_configured_llm(tmp_path: Path, field_name: str) -> None:
    maintenance = importlib.import_module("hl_mem.workers.maintenance")
    database = Database(tmp_path / f"missing-llm-{field_name}.db")
    connection = database.open()
    settings = replace(Settings.for_test(), **{field_name: True})

    assert maintenance.build_semantic_schedules(connection, settings, now=lambda: NOW) == []
    database.close()
