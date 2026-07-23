"""统一搜索追踪的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from hl_mem.application.recall import RecallService
from hl_mem.ingest.embeddings import FakeEmbedder, pack_vector
from hl_mem.recall.recall_pipeline import hybrid_claims
from hl_mem.recall.trace import SearchPhaseMetrics, SearchTrace, SearchTracer
from hl_mem.storage.database import Database


def _claim(claim_id: str, *, status: str = "active") -> dict[str, object]:
    return {
        "id": claim_id,
        "subject_entity_id": "user",
        "predicate": "preference",
        "value_json": '"secret value"',
        "embedding_dense": pack_vector([1.0]),
        "status": status,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "recorded_to": None,
        "confidence": 1.0,
        "importance": 0.5,
        "access_count": 0,
    }


class _Repo:
    def __init__(self) -> None:
        self.claims = [_claim("first"), _claim("filtered", status="withdrawn"), _claim("last")]

    def search_claims_fts(self, *_args, **_kwargs):
        return self.claims

    def search_claims_vector(self, *_args, **_kwargs):
        return list(reversed(self.claims))

    def helpful_rates(self, _claim_ids):
        return {}


def _tracer(limit: int = 1) -> SearchTracer:
    return SearchTracer(
        SearchTrace(
            query_id="query-1",
            query_hash="hash-only",
            intent="current_state",
            limit=limit,
            candidate_limit=50,
            candidates={},
            phases=SearchPhaseMetrics(),
        )
    )


def test_search_tracer_serializes_ranks_scores_filters_and_timings() -> None:
    tracer = _tracer()
    tracer.record_channel("fts", [{"id": "first", "_score": 0.9}, {"id": "second", "_score": 0.5}])
    tracer.record_filter("second", "status_filtered")
    tracer.record_pre_rank([{"id": "first"}], {"first": 0.75})
    tracer.record_rerank([("first", 0.8)])
    tracer.record_final([{"id": "first"}])
    tracer.trace.phases.fts_us = 12
    tracer.trace.phases.total_us = 34

    payload = tracer.to_dict()

    assert payload["candidates"]["first"]["channels"] == {"fts": 1}
    assert payload["candidates"]["first"]["channel_scores"] == {"fts": 0.9}
    assert payload["candidates"]["first"]["pre_rank"] == 1
    assert payload["candidates"]["first"]["rerank_rank"] == 1
    assert payload["candidates"]["first"]["final_rank"] == 1
    assert payload["candidates"]["first"]["included"] is True
    assert payload["candidates"]["second"]["filter_reasons"] == ["status_filtered"]
    assert payload["phases"]["fts_us"] == 12
    json.dumps(payload)


def test_hybrid_claims_records_candidates_without_sensitive_text() -> None:
    tracer = _tracer()

    results = hybrid_claims(
        _Repo(),
        "plaintext query",
        pack_vector([1.0]),
        1,
        None,
        now="2026-07-24T00:00:00+00:00",
        tracer=tracer,
    )
    serialized = json.dumps(tracer.to_dict())

    assert [claim["id"] for claim in results] == ["first"]
    assert tracer.to_dict()["candidates"]["filtered"]["filter_reasons"] == ["status_filtered"]
    assert tracer.to_dict()["candidates"]["last"]["filter_reasons"] == ["final_limit"]
    assert tracer.to_dict()["phases"]["fusion_us"] >= 0
    assert "plaintext query" not in serialized
    assert "secret value" not in serialized


def test_search_tracer_truncates_non_final_candidates() -> None:
    tracer = SearchTracer(
        SearchTrace(
            query_id="query-1",
            query_hash="hash-only",
            intent="current_state",
            limit=1,
            candidate_limit=50,
            candidates={},
            phases=SearchPhaseMetrics(),
        ),
        max_candidates=2,
    )
    tracer.record_channel("fts", [{"id": "first"}, {"id": "second"}, {"id": "third"}])
    tracer.record_final([{"id": "third"}])

    payload = tracer.to_dict()

    assert payload["truncated"] is True
    assert set(payload["candidates"]) == {"first", "third"}


def test_recall_service_only_returns_search_trace_in_debug_mode(tmp_path: Path) -> None:
    database = Database(tmp_path / "search-trace.db")
    try:
        with database.connect() as connection:
            service = RecallService(connection, FakeEmbedder(4))

            normal = service.recall("private query")
            debug = service.recall("private query", debug=True, query_id="query-1")
    finally:
        database.close()

    assert "search_trace" not in normal
    assert debug["search_trace"]["query_id"] == "query-1"
    assert debug["search_trace"]["query_hash"] != "private query"
    assert "private query" not in json.dumps(debug["search_trace"])
