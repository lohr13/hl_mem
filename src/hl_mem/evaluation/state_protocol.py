"""Shared value and counting protocol for structured state evaluation."""

from __future__ import annotations

import json
from collections.abc import Collection, Hashable, Mapping
from dataclasses import dataclass
from typing import Any

from hl_mem.domain.claims.state_coordinates import StateCoordinate


def rate(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    """Return one ratio with an explicit empty-denominator result."""

    return numerator / denominator if denominator else empty


def coordinate_from_mapping(value: Mapping[str, Any]) -> StateCoordinate:
    """Validate an already-structured coordinate without inferring semantics."""

    qualifiers = value.get("coordinate_qualifiers", {})
    if not isinstance(qualifiers, Mapping):
        raise TypeError("coordinate_qualifiers must be a mapping")
    return StateCoordinate(
        namespace=value.get("namespace"),
        canonical_subject=value.get("canonical_subject"),
        canonical_slot=value.get("canonical_slot"),
        coordinate_qualifiers=qualifiers,
    )


def coordinate_mapping(coordinate: StateCoordinate) -> dict[str, Any]:
    """Return the stable JSON-compatible representation of a coordinate."""

    return {
        "namespace": coordinate.namespace,
        "canonical_subject": coordinate.canonical_subject,
        "canonical_slot": coordinate.canonical_slot,
        "coordinate_qualifiers": {
            key: json.loads(frozen_json) for key, frozen_json in coordinate.coordinate_qualifiers
        },
    }


def coordinate_key(value: StateCoordinate | Mapping[str, Any]) -> str:
    """Return one stable key for any already-structured coordinate source."""

    coordinate = value if isinstance(value, StateCoordinate) else coordinate_from_mapping(value)
    return json.dumps(
        coordinate_mapping(coordinate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class CountMetrics:
    """Immutable TP/FP/FN ledger with the frozen empty-set rate rules."""

    true_positive: int
    false_positive: int
    false_negative: int

    def __post_init__(self) -> None:
        values = (self.true_positive, self.false_positive, self.false_negative)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("metric counts must be non-negative integers")

    @property
    def precision(self) -> float:
        predicted = self.true_positive + self.false_positive
        return rate(self.true_positive, predicted, empty=float(self.false_negative == 0))

    @property
    def recall(self) -> float:
        gold = self.true_positive + self.false_negative
        return rate(self.true_positive, gold, empty=float(self.false_positive == 0))

    @property
    def f1(self) -> float:
        return (
            2 * self.precision * self.recall / (self.precision + self.recall) if self.precision + self.recall else 0.0
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "false_negative": self.false_negative,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }

    @classmethod
    def classify(
        cls,
        gold: Collection[Hashable],
        predicted: Collection[Hashable],
    ) -> CountMetrics:
        gold_set = set(gold)
        predicted_set = set(predicted)
        return cls(
            true_positive=len(gold_set & predicted_set),
            false_positive=len(predicted_set - gold_set),
            false_negative=len(gold_set - predicted_set),
        )
