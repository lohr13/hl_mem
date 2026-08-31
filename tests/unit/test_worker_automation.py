from __future__ import annotations

import importlib
from dataclasses import replace

import pytest

from hl_mem.settings import Settings


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
def test_semantic_jobs_are_independently_disabled_by_default(
    job_type: str,
    field_name: str,
    enabled_value: bool | str,
) -> None:
    automation = importlib.import_module("hl_mem.workers.automation")
    settings = Settings.for_test()

    assert automation.semantic_job_enabled(settings, job_type) is False
    assert automation.semantic_job_enabled(replace(settings, **{field_name: enabled_value}), job_type) is True


def test_semantic_job_policy_rejects_unknown_job_type() -> None:
    automation = importlib.import_module("hl_mem.workers.automation")

    with pytest.raises(ValueError, match="unknown semantic job type"):
        automation.semantic_job_enabled(Settings.for_test(), "unregistered")
