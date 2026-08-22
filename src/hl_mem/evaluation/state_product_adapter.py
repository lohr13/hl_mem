"""Fail-closed evidence binding between raw extraction and production claims."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from hl_mem.ingest.admission import evidence_quote_matches


class BoundProductEvidence(NamedTuple):
    product_claim: Mapping[str, Any]
    raw_claims: tuple[Mapping[str, Any], ...]
    raw_claim_indices: tuple[int, ...]

    @property
    def raw_claim(self) -> Mapping[str, Any]:
        """Return the representative raw claim for single-quote scorers."""

        return self.raw_claims[0]

    @property
    def raw_claim_index(self) -> int:
        """Return the representative raw index for backward compatibility."""

        return self.raw_claim_indices[0]


class _NormalizedRaw(NamedTuple):
    raw_index: int
    claim: Mapping[str, Any]
    indices: tuple[int, ...] | None
    value: str
    merge_identity: tuple[str, str, str, str]


def _indices(value: object, *, event_count: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} source_event_indices must be an integer array")
    indices = list(value)
    if not indices or any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= event_count for index in indices
    ):
        raise ValueError(f"{label} source_event_indices must reference existing events")
    return tuple(sorted(set(indices)))


def _raw_indices(raw_claim: Mapping[str, Any], *, event_count: int, label: str) -> tuple[int, ...] | None:
    if "source_event_indices" not in raw_claim:
        return (0,) if event_count == 1 else None
    return _indices(raw_claim["source_event_indices"], event_count=event_count, label=label)


def _normalized_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _merge_identity(raw_claim: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _normalized_text(raw_claim.get("subject")),
        _normalized_text(raw_claim.get("kind") or raw_claim.get("predicate")),
        _normalized_text(raw_claim.get("assertion_kind")),
        _normalized_text(raw_claim.get("evidence_quote")),
    )


def _event_texts(value: Sequence[str] | None, *, event_count: int) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or len(value) != event_count or any(not isinstance(item, str) for item in value):
        raise ValueError("source_event_texts must contain one string per event")
    return tuple(value)


def _validate_group_grounding(group: Sequence[_NormalizedRaw], source_event_texts: tuple[str, ...]) -> None:
    for row in group:
        assert row.indices is not None
        evidence_quote = str(row.claim.get("evidence_quote") or "").strip()
        source_text = "\n".join(source_event_texts[index] for index in row.indices)
        if not evidence_quote_matches(evidence_quote, source_text):
            raise ValueError(f"raw claim {row.raw_index} evidence_quote is not grounded in its source events")


def bind_product_evidence(
    raw_claims: Sequence[Mapping[str, Any]],
    product_claims: Sequence[Mapping[str, Any]],
    *,
    event_count: int,
    source_event_texts: Sequence[str] | None = None,
) -> list[BoundProductEvidence]:
    """Bind exact or uniquely merged raw evidence without weakening source grounding."""

    if event_count <= 0:
        raise ValueError("event_count must be positive")
    normalized_event_texts = _event_texts(source_event_texts, event_count=event_count)
    normalized_raw = [
        _NormalizedRaw(
            raw_index=index,
            claim=raw,
            indices=_raw_indices(raw, event_count=event_count, label=f"raw claim {index}"),
            value=str(raw.get("value") or "").strip(),
            merge_identity=_merge_identity(raw),
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
        available = [row for row in normalized_raw if row.raw_index not in used and row.value == product_value]
        exact = [row for row in available if row.indices == product_indices]
        if len(exact) > 1:
            raise ValueError(f"product claim {product_index} has ambiguous raw evidence")
        if exact:
            matched = exact
        else:
            if any(row.indices is None for row in available):
                raise ValueError("raw claim omits source_event_indices for a multi-event bundle")
            groups: dict[tuple[str, str, str, str], list[_NormalizedRaw]] = {}
            for row in available:
                groups.setdefault(row.merge_identity, []).append(row)
            composite = [
                group
                for group in groups.values()
                if len(group) > 1
                and tuple(sorted({index for row in group for index in (row.indices or ())})) == product_indices
            ]
            if len(composite) > 1:
                raise ValueError(f"product claim {product_index} has ambiguous composite raw evidence")
            if not composite:
                raise ValueError("product claim cannot be matched to its source-bounded raw evidence")
            if normalized_event_texts is None:
                raise ValueError("composite evidence binding requires source_event_texts")
            matched = composite[0]
            _validate_group_grounding(matched, normalized_event_texts)
        matched_indices = tuple(row.raw_index for row in matched)
        used.update(matched_indices)
        bindings.append(
            BoundProductEvidence(
                product_claim=product,
                raw_claims=tuple(row.claim for row in matched),
                raw_claim_indices=matched_indices,
            )
        )
    return bindings
