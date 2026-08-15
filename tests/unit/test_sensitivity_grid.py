from __future__ import annotations

import pytest

from hl_mem.evaluation.sensitivity import sensitivity_weight_variants
from hl_mem.recall.ranking import DEFAULT_WEIGHTS


def test_sensitivity_grid_freezes_all_single_factor_and_top_hat_variants() -> None:
    variants = sensitivity_weight_variants(DEFAULT_WEIGHTS)

    assert len(variants) == 16
    assert set(variants) >= {
        "baseline",
        "semantic_x0.5",
        "semantic_x1.5",
        "utility_x0.5",
        "utility_x1.5",
        "semantic_cap_balanced",
        "semantic_cap_freshness",
        "semantic_cap_quality",
    }
    assert all(sum(weights.values()) == pytest.approx(1.0) for weights in variants.values())


def test_top_hat_variants_cap_semantic_and_redistribute_only_as_registered() -> None:
    variants = sensitivity_weight_variants(DEFAULT_WEIGHTS)

    balanced = variants["semantic_cap_balanced"]
    freshness = variants["semantic_cap_freshness"]
    quality = variants["semantic_cap_quality"]
    assert balanced["semantic"] == pytest.approx(0.55)
    assert freshness["semantic"] == pytest.approx(0.55)
    assert quality["semantic"] == pytest.approx(0.55)
    assert freshness["confidence"] == DEFAULT_WEIGHTS["confidence"]
    assert freshness["importance"] == DEFAULT_WEIGHTS["importance"]
    assert freshness["utility"] == DEFAULT_WEIGHTS["utility"]
    assert quality["recency"] == DEFAULT_WEIGHTS["recency"]
    assert quality["access_frequency"] == DEFAULT_WEIGHTS["access_frequency"]
