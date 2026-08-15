from __future__ import annotations

import pytest

from hl_mem.evaluation.metrics import cluster_bootstrap_ci, paired_cluster_bootstrap_ci


def test_cluster_bootstrap_is_deterministic_and_keeps_clusters_together() -> None:
    values = [1.0, 1.0, 0.0, 0.0]
    clusters = ["persona-a", "persona-a", "persona-b", "persona-b"]

    first = cluster_bootstrap_ci(values, clusters, seed=17, resamples=2000)
    second = cluster_bootstrap_ci(values, clusters, seed=17, resamples=2000)

    assert first == second
    assert first == pytest.approx((0.0, 1.0))


def test_paired_cluster_bootstrap_resamples_treatment_minus_control() -> None:
    control = [0.0, 1.0, 0.0, 1.0]
    treatment = [1.0, 1.0, 0.0, 0.0]
    clusters = ["trajectory-a", "trajectory-a", "trajectory-b", "trajectory-b"]

    interval = paired_cluster_bootstrap_ci(
        control,
        treatment,
        clusters,
        seed=23,
        resamples=2000,
    )

    assert interval == pytest.approx((-0.5, 0.5))


@pytest.mark.parametrize(
    ("values", "clusters"),
    [([], []), ([1.0], []), ([1.0, 2.0], ["a"]), ([1.0], [""])],
)
def test_cluster_bootstrap_rejects_invalid_samples(values: list[float], clusters: list[str]) -> None:
    with pytest.raises(ValueError):
        cluster_bootstrap_ci(values, clusters)


def test_paired_cluster_bootstrap_requires_aligned_pairs() -> None:
    with pytest.raises(ValueError):
        paired_cluster_bootstrap_ci([1.0], [1.0, 2.0], ["a"])
