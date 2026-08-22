from __future__ import annotations

import importlib
from typing import Any

import pytest


@pytest.fixture(scope="module")
def smoke_result() -> dict[str, Any]:
    assert importlib.util.find_spec("hl_mem.evaluation.smoke_full_chain") is not None
    module = importlib.import_module("hl_mem.evaluation.smoke_full_chain")
    return module.run_full_chain_smoke()


def test_full_chain_smoke_keeps_uncontracted_snapshots_out_of_state_projection(
    smoke_result: dict[str, Any],
) -> None:
    assert smoke_result["seams"]["state_snapshot_not_projected"] is True


def test_full_chain_smoke_applies_the_single_event_source_index_default(smoke_result: dict[str, Any]) -> None:
    assert smoke_result["seams"]["default_source_index"] is True


def test_full_chain_smoke_binds_both_sources_after_product_deduplication(smoke_result: dict[str, Any]) -> None:
    assert smoke_result["seams"]["composite_binding"] is True


def test_full_chain_smoke_keeps_state_resolver_inactive_and_produces_all_thirteen_checks(
    smoke_result: dict[str, Any],
) -> None:
    assert smoke_result["seams"]["resolver_state_edge_absent"] is True
    assert smoke_result["check_count"] == 13
    assert set(smoke_result["checks"]) == {
        "state_coordinate_precision",
        "state_coordinate_recall",
        "atomic_claim_precision",
        "atomic_claim_recall",
        "supersede_edge_precision",
        "supersede_edge_recall",
        "counterexample_cross_coordinate_supersede",
        "stale_injection_reduction",
        "stale_injection_absolute",
        "historical_old_snapshot_recall",
        "non_state_f1_drop",
        "claim_inflation",
        "three_run_coordinate_consistency",
    }
