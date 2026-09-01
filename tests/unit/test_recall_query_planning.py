from __future__ import annotations

from dataclasses import replace

from hl_mem.application.recall import RecallRequest, RecallService
from hl_mem.domain.recall import RecallIntent
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.query_planning import PreparedQueries, QueryPlanningSession
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


class _RecordingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__(4)
        self.calls: list[str] = []

    def embed_one(self, text: str) -> bytes:
        self.calls.append(text)
        return super().embed_one(text)


def _request(query: str) -> RecallRequest:
    return RecallRequest(
        query=query,
        limit=5,
        as_of=None,
        intent=RecallIntent.CURRENT_STATE,
        known_as_of=None,
        query_id="query-planning-direct",
        token_budget=None,
        context_mode=None,
        namespace="default",
        session_id=None,
        debug=True,
        response_format="legacy",
        ranking_now="2026-09-01T00:00:00+00:00",
        injection_context=None,
    )


def test_query_planning_session_returns_an_immutable_prepared_snapshot(tmp_path) -> None:
    database = Database(tmp_path / "query-planning.db")
    connection = database.open()
    embedder = _RecordingEmbedder()
    settings = replace(
        Settings.for_test(),
        embedding_dim=4,
        query_expansion_mode="off",
        entity_constraint_mode="off",
    )
    service = RecallService(connection, embedder, settings=settings)
    recall = service._resolve_recall_request(_request("tea preference"))

    prepared = QueryPlanningSession(service, recall).prepare()

    assert isinstance(prepared, PreparedQueries)
    assert [(item.text, item.source, item.weight) for item in prepared.weighted_queries] == [
        ("tea preference", "original", 1.0)
    ]
    assert len(prepared.query_blobs) == 1
    assert prepared.entity_plan.scope_mode == "off"
    assert prepared.low_recall_expander is None
    assert embedder.calls == ["tea preference"]
    database.close()
