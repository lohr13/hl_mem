from __future__ import annotations

import sqlite3

import pytest

from hl_mem.domain.temporal import RecallIntent
from hl_mem.protocols import WeightedQuery
from hl_mem.recall.candidate_channels import ChannelRequest, collect_query_channels


def _claim(claim_id: str, entity_id: str | None) -> dict[str, object]:
    return {
        "id": claim_id,
        "subject_canonical_entity_id": entity_id,
        "canonical_target_entity_id": None,
        "_score": 1.0,
    }


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE claim_entity_links(claim_id TEXT, canonical_entity_id TEXT)")
    return connection


def _request(mode: str, entity_id: str | None) -> ChannelRequest:
    return ChannelRequest(
        candidate_limit=5,
        reference="2026-01-01T00:00:00+00:00",
        selected_intent=RecallIntent.CURRENT_STATE,
        known_as_of=None,
        namespace="default",
        dense_enabled=True,
        entity_scope_mode=mode,
        entity_scope_id=entity_id,
    )


class _RecordingRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, str | None]] = []
        self.claims = [_claim("other", "agent:other"), _claim("target", "agent:target")]

    def search_claims_fts(self, *_args: object, **kwargs: object) -> list[dict[str, object]]:
        entity_id = kwargs.get("entity_id")
        self.calls.append(("fts", str(entity_id) if entity_id is not None else None))
        return [
            claim for claim in self.claims if entity_id is None or claim["subject_canonical_entity_id"] == entity_id
        ]

    def search_claims_vector(self, *_args: object, **kwargs: object) -> list[dict[str, object]]:
        entity_id = kwargs.get("entity_id")
        self.calls.append(("dense", str(entity_id) if entity_id is not None else None))
        return [
            claim for claim in self.claims if entity_id is None or claim["subject_canonical_entity_id"] == entity_id
        ]


def test_enforce_pushes_scope_into_both_existing_channels_before_results_return() -> None:
    repository = _RecordingRepository(_connection())

    collected = collect_query_channels(
        repository,
        WeightedQuery("deployment", "original", 1.0),
        b"vector",
        0,
        _request("enforce", "agent:target"),
    )

    assert repository.calls == [("fts", "agent:target"), ("dense", "agent:target")]
    assert [[claim["id"] for claim in channel] for _, channel, _, _ in collected.channels] == [
        ["target"],
        ["target"],
    ]
    assert collected.entity_scope_applied is True
    assert collected.entity_scope_counts == {"fts": 1, "dense": 1}
    assert collected.filtered_ids == frozenset()


def test_observe_reads_wide_and_keeps_shadow_filter_instrumentation() -> None:
    repository = _RecordingRepository(_connection())

    collected = collect_query_channels(
        repository,
        WeightedQuery("deployment", "original", 1.0),
        b"vector",
        0,
        _request("observe", "agent:target"),
    )

    assert repository.calls == [("fts", None), ("dense", None)]
    assert all([claim["id"] for claim in channel] == ["other", "target"] for _, channel, _, _ in collected.channels)
    assert collected.entity_scope_applied is False
    assert collected.entity_scope_counts == {"fts": 2, "dense": 2}
    assert collected.filtered_ids == frozenset({"other"})


def test_scoped_storage_error_returns_control_without_an_internal_wide_read() -> None:
    class FailingScopedRepository(_RecordingRepository):
        def search_claims_fts(self, *_args: object, **kwargs: object) -> list[dict[str, object]]:
            entity_id = kwargs.get("entity_id")
            self.calls.append(("fts", str(entity_id) if entity_id is not None else None))
            if entity_id is not None:
                raise sqlite3.OperationalError("scoped read failed")
            return self.claims

    repository = FailingScopedRepository(_connection())

    with pytest.raises(sqlite3.OperationalError, match="scoped read failed"):
        collect_query_channels(
            repository,
            WeightedQuery("deployment", "original", 1.0),
            b"vector",
            0,
            _request("enforce", "agent:target"),
        )

    assert repository.calls == [("fts", "agent:target")]


def test_scoped_dense_error_does_not_issue_an_internal_wide_read() -> None:
    class FailingScopedDenseRepository(_RecordingRepository):
        def search_claims_vector(self, *_args: object, **kwargs: object) -> list[dict[str, object]]:
            entity_id = kwargs.get("entity_id")
            self.calls.append(("dense", str(entity_id) if entity_id is not None else None))
            if entity_id is not None:
                raise sqlite3.OperationalError("scoped dense read failed")
            return self.claims

    repository = FailingScopedDenseRepository(_connection())

    with pytest.raises(sqlite3.OperationalError, match="scoped dense read failed"):
        collect_query_channels(
            repository,
            WeightedQuery("deployment", "original", 1.0),
            b"vector",
            0,
            _request("enforce", "agent:target"),
        )

    assert repository.calls == [
        ("fts", "agent:target"),
        ("dense", "agent:target"),
    ]


def test_unscoped_storage_error_propagates_without_retry() -> None:
    class AlwaysFailingRepository(_RecordingRepository):
        def search_claims_fts(self, *_args: object, **kwargs: object) -> list[dict[str, object]]:
            entity_id = kwargs.get("entity_id")
            self.calls.append(("fts", str(entity_id) if entity_id is not None else None))
            raise sqlite3.OperationalError("wide failure")

    repository = AlwaysFailingRepository(_connection())
    with pytest.raises(sqlite3.OperationalError, match="wide failure"):
        collect_query_channels(
            repository,
            WeightedQuery("deployment", "original", 1.0),
            b"vector",
            0,
            _request("wide", None),
        )
    assert repository.calls == [("fts", None)]
