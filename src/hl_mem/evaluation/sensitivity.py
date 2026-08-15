"""Frozen weight variants for deterministic multi-factor sensitivity replay."""

from __future__ import annotations

from collections.abc import Mapping

FACTOR_NAMES = (
    "semantic",
    "recency",
    "access_frequency",
    "confidence",
    "importance",
    "utility",
)


def _normalize(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(float(weights[name]) for name in FACTOR_NAMES)
    if total <= 0.0:
        raise ValueError("weight total must be positive")
    return {name: float(weights[name]) / total for name in FACTOR_NAMES}


def _semantic_cap(
    baseline: Mapping[str, float],
    recipients: tuple[str, ...],
    *,
    cap: float = 0.55,
) -> dict[str, float]:
    weights = {name: float(baseline[name]) for name in FACTOR_NAMES}
    released = weights["semantic"] - cap
    if released < 0.0:
        raise ValueError("semantic cap must not exceed the baseline")
    recipient_total = sum(weights[name] for name in recipients)
    if recipient_total <= 0.0:
        raise ValueError("recipient weights must be positive")
    weights["semantic"] = cap
    for name in recipients:
        weights[name] += released * float(baseline[name]) / recipient_total
    return _normalize(weights)


def sensitivity_weight_variants(baseline: Mapping[str, float]) -> dict[str, dict[str, float]]:
    """Return the preregistered ±50% and semantic-cap variants."""

    missing = [name for name in FACTOR_NAMES if name not in baseline]
    if missing:
        raise ValueError(f"missing weights: {', '.join(missing)}")
    variants = {"baseline": _normalize(baseline)}
    for name in FACTOR_NAMES:
        for multiplier in (0.5, 1.5):
            changed = {factor: float(baseline[factor]) for factor in FACTOR_NAMES}
            changed[name] *= multiplier
            variants[f"{name}_x{multiplier:.1f}"] = _normalize(changed)
    variants["semantic_cap_balanced"] = _semantic_cap(
        baseline,
        tuple(name for name in FACTOR_NAMES if name != "semantic"),
    )
    variants["semantic_cap_freshness"] = _semantic_cap(
        baseline,
        ("recency", "access_frequency"),
    )
    variants["semantic_cap_quality"] = _semantic_cap(
        baseline,
        ("confidence", "importance", "utility"),
    )
    return variants
