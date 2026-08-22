"""Fail-closed evidence binding between raw extraction and production claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple


class BoundProductEvidence(NamedTuple):
    product_claim: Mapping[str, Any]
    raw_claim: Mapping[str, Any]
    raw_claim_index: int


def _indices(value: object, *, event_count: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} source_event_indices must be an integer array")
    indices = list(value)
    if not indices or any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= event_count for index in indices
    ):
        raise ValueError(f"{label} source_event_indices must reference existing events")
    return tuple(dict.fromkeys(indices))


def _raw_indices(raw_claim: Mapping[str, Any], *, event_count: int, label: str) -> tuple[int, ...] | None:
    if "source_event_indices" not in raw_claim:
        return (0,) if event_count == 1 else None
    return _indices(raw_claim["source_event_indices"], event_count=event_count, label=label)


def bind_product_evidence(
    raw_claims: Sequence[Mapping[str, Any]],
    product_claims: Sequence[Mapping[str, Any]],
    *,
    event_count: int,
) -> list[BoundProductEvidence]:
    """Bind by exact source/value; only single-event omission defaults to zero."""

    if event_count <= 0:
        raise ValueError("event_count must be positive")
    normalized_raw = [
        (
            index,
            raw,
            _raw_indices(raw, event_count=event_count, label=f"raw claim {index}"),
            str(raw.get("value") or "").strip(),
        )
        for index, raw in enumerate(raw_claims)
    ]

    used: set[int] = set()
    bindings: list[BoundProductEvidence] = []
    for product_index, product in enumerate(product_claims):
        product_indices = _indices(
            product.get("source_event_indices"),
            event_count=event_count,
            label=f"product claim {product_index}",
        )
        product_value = str(product.get("value") or "").strip()
        if not product_value:
            raise ValueError(f"product claim {product_index} value must be non-blank")
        available = [row for row in normalized_raw if row[0] not in used and row[3] == product_value]
        exact = [(index, raw) for index, raw, indices, _ in available if indices == product_indices]
        if len(exact) > 1:
            raise ValueError(f"product claim {product_index} has ambiguous raw evidence")
        if not exact:
            if any(indices is None for _, _, indices, _ in available):
                raise ValueError("raw claim omits source_event_indices for a multi-event bundle")
            raise ValueError("product claim cannot be matched to its source-bounded raw evidence")
        raw_index, raw = exact[0]
        used.add(raw_index)
        bindings.append(BoundProductEvidence(product, raw, raw_index))
    return bindings
