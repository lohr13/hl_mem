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
    entity_constraint_mode: str
    entity_filter_id: str | None


@dataclass(frozen=True, slots=True)
class CollectedChannels:
    channels: tuple[tuple[str, list[dict[str, Any]], float, float], ...]
    fts_us: int
    dense_us: int
    filtered_ids: frozenset[str]


def collect_query_channels(
    repo: Any,
    item: WeightedQuery,
    blob: bytes,
    index: int,
    request: ChannelRequest,
) -> CollectedChannels:
    """Collect one weighted query without introducing a new retrieval channel or score."""

    label = "original" if index == 0 else f"expansion_{index}"
    started = time.perf_counter_ns()
    raw_fts = [
        dict(claim)
        for claim in repo.search_claims_fts(
            item.text,
            request.candidate_limit,
            request.reference,
            request.selected_intent,
            request.known_as_of,
            namespace=request.namespace,
        )
    ]
    fts_us = (time.perf_counter_ns() - started) // 1000
    fts = apply_entity_constraint(
        getattr(repo, "connection", None),
        raw_fts,
        request.entity_constraint_mode,
        request.entity_filter_id,
    )
    channels = [(f"{label}:fts", fts.items, item.weight, 1.0)]
    dense_us = 0
    filtered_ids = set(fts.filtered_ids)
    if request.dense_enabled:
        started = time.perf_counter_ns()
        raw_dense = [
            dict(claim)
            for claim in repo.search_claims_vector(
                blob,
                request.candidate_limit,
                request.reference,
                request.selected_intent,
                request.known_as_of,
                namespace=request.namespace,
            )
        ]
        dense_us = (time.perf_counter_ns() - started) // 1000
        dense = apply_entity_constraint(
            getattr(repo, "connection", None),
            raw_dense,
            request.entity_constraint_mode,
            request.entity_filter_id,
        )
        channels.append((f"{label}:dense", dense.items, item.weight, 1.0))
        filtered_ids.update(dense.filtered_ids)
    return CollectedChannels(tuple(channels), fts_us, dense_us, frozenset(filtered_ids))
