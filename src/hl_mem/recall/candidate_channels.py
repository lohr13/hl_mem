"""Bounded FTS/dense query-channel collection outside the ranking pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hl_mem.protocols import WeightedQuery
from hl_mem.recall.entity_query import apply_entity_constraint


@dataclass(frozen=True, slots=True)
class ChannelRequest:
    candidate_limit: int
    reference: str
    selected_intent: Any
    known_as_of: str | None
    namespace: str
    dense_enabled: bool
    entity_scope_mode: str
    entity_scope_id: str | None


@dataclass(frozen=True, slots=True)
class CollectedChannels:
    channels: tuple[tuple[str, list[dict[str, Any]], float, float], ...]
    fts_us: int
    dense_us: int
    filtered_ids: frozenset[str]
    entity_scope_applied: bool
    entity_scope_counts: dict[str, int]
    entity_scope_us: int


def collect_query_channels(
    repo: Any,
    item: WeightedQuery,
    blob: bytes,
    index: int,
    request: ChannelRequest,
) -> CollectedChannels:
    """Collect one weighted query without introducing a new retrieval channel or score."""

    label = "original" if index == 0 else f"expansion_{index}"
    scoped = request.entity_scope_mode in {"entity", "enforce"} and request.entity_scope_id is not None
    scope_started = time.perf_counter_ns() if scoped else 0

    def read(entity_id: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        started = time.perf_counter_ns()
        fts = [
            dict(claim)
            for claim in repo.search_claims_fts(
                item.text,
                request.candidate_limit,
                request.reference,
                request.selected_intent,
                request.known_as_of,
                namespace=request.namespace,
                entity_id=entity_id,
            )
        ]
        fts_us = (time.perf_counter_ns() - started) // 1000
        dense: list[dict[str, Any]] = []
        dense_us = 0
        if request.dense_enabled:
            started = time.perf_counter_ns()
            dense = [
                dict(claim)
                for claim in repo.search_claims_vector(
                    blob,
                    request.candidate_limit,
                    request.reference,
                    request.selected_intent,
                    request.known_as_of,
                    namespace=request.namespace,
                    entity_id=entity_id,
                )
            ]
            dense_us = (time.perf_counter_ns() - started) // 1000
        return fts, dense, fts_us, dense_us

    raw_fts, raw_dense, fts_us, dense_us = read(request.entity_scope_id if scoped else None)
    entity_scope_us = (time.perf_counter_ns() - scope_started) // 1000 if scoped else 0

    filtered_ids: set[str] = set()
    if request.entity_scope_mode == "observe" and request.entity_scope_id is not None:
        shadow_fts = apply_entity_constraint(
            getattr(repo, "connection", None),
            raw_fts,
            "observe",
            request.entity_scope_id,
        )
        filtered_ids.update(shadow_fts.filtered_ids)
        if request.dense_enabled:
            shadow_dense = apply_entity_constraint(
                getattr(repo, "connection", None),
                raw_dense,
                "observe",
                request.entity_scope_id,
            )
            filtered_ids.update(shadow_dense.filtered_ids)

    channels = [(f"{label}:fts", raw_fts, item.weight, 1.0)]
    counts = {"fts": len(raw_fts)}
    if request.dense_enabled:
        channels.append((f"{label}:dense", raw_dense, item.weight, 1.0))
        counts["dense"] = len(raw_dense)
    return CollectedChannels(
        tuple(channels),
        fts_us,
        dense_us,
        frozenset(filtered_ids),
        scoped,
        counts,
        entity_scope_us,
    )
