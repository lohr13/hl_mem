"""会话感知查询改写的隔离、降级与契约测试。"""

from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.application.recall import RecallService, _session_context
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.llm.types import LLMResponse
from hl_mem.recall.query_expansion import QueryExpander
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


class _CapturingClient:
    """记录查询改写请求的测试客户端。"""

    model = "fake-model"

    def __init__(self) -> None:
        self.requests: list[object] = []

    def complete(self, request: object) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(content={"queries": ["生产环境部署方案"]}, finish_reason="stop")


def _insert_message(
    repository: EventRepository,
    event_id: str,
    namespace: str,
    session_id: str,
    text: str,
    actor_type: str = "user",
) -> None:
    """插入一条会话消息。"""
    repository.insert_event(
        {
            "id": event_id,
            "tenant_id": namespace,
            "session_id": session_id,
            "event_type": "message",
            "actor_type": actor_type,
            "content_json": json.dumps(
                {"text": text, "internal": "不得进入 prompt"},
                ensure_ascii=False,
            ),
            "occurred_at": f"2026-07-29T10:00:0{event_id[-1]}+00:00",
            "recorded_at": f"2026-07-29T10:00:0{event_id[-1]}+00:00",
        }
    )


def test_rest_passes_session_id_to_recall_service(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _RecallService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def recall(self, *args: object, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {
                "results": [],
                "observations": [],
                "policies": [],
                "total": 0,
                "query_id": "q",
            }

    monkeypatch.setattr(server, "RecallService", _RecallService)
    with TestClient(server.create_app(tmp_path / "api.db")) as client:
        response = client.post("/v1/recall", json={"query": "这个方案", "session_id": "session-1"})

    assert response.status_code == 200
    assert captured["session_id"] == "session-1"


def test_session_context_isolated_and_contains_only_text(tmp_path) -> None:
    database = Database(tmp_path / "context.db")
    with database.connect() as connection:
        repository = EventRepository(connection)
        _insert_message(repository, "event-1", "alpha", "same", "alpha 文本")
        _insert_message(repository, "event-2", "beta", "same", "beta 文本")

        context, truncated, context_hash, outcome = _session_context(
            connection,
            "alpha",
            "same",
            max_events=5,
            token_budget=256,
        )

    assert context == (("user", "alpha 文本"),)
    assert truncated is False
    assert context_hash is not None and len(context_hash) == 64
    assert outcome == "ok"
    assert "internal" not in json.dumps(context, ensure_ascii=False)


def test_session_context_filters_roles_before_limit(tmp_path) -> None:
    """system/tool 事件不得占用最近对话的 LIMIT。"""
    database = Database(tmp_path / "context-roles.db")
    with database.connect() as connection:
        repository = EventRepository(connection)
        _insert_message(repository, "event-1", "alpha", "same", "有效消息")
        _insert_message(repository, "event-2", "alpha", "same", "工具消息", "tool")
        _insert_message(repository, "event-3", "alpha", "same", "系统消息", "system")

        context, truncated, _, outcome = _session_context(
            connection,
            "alpha",
            "same",
            max_events=1,
            token_budget=256,
        )

    assert context == (("user", "有效消息"),)
    assert truncated is False
    assert outcome == "ok"


def test_session_context_skips_oversized_message_and_keeps_earlier_short_message(tmp_path) -> None:
    """最近消息超预算时继续尝试装入更早的短消息。"""
    database = Database(tmp_path / "context-budget.db")
    with database.connect() as connection:
        repository = EventRepository(connection)
        _insert_message(repository, "event-1", "alpha", "same", "短消息")
        _insert_message(repository, "event-2", "alpha", "same", "超长" * 100)

        context, truncated, _, outcome = _session_context(
            connection,
            "alpha",
            "same",
            max_events=2,
            token_budget=10,
        )

    assert context == (("user", "短消息"),)
    assert truncated is True
    assert outcome == "ok"


def test_coreference_context_reaches_expander_but_not_trace(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "recall.db")
    client = _CapturingClient()
    settings = replace(
        Settings(),
        query_expansion_mode="auto",
        query_context_mode="coreference",
    )
    with database.connect() as connection:
        _insert_message(EventRepository(connection), "event-1", "default", "session-1", "讨论生产环境部署方案")
        monkeypatch.setattr("hl_mem.application.recall.hybrid_claims", lambda *args, **kwargs: [])
        response = RecallService(
            connection,
            FakeEmbedder(4),
            settings=settings,
            query_expander=QueryExpander(client),
        ).recall("之前讨论的那个方案", session_id="session-1", debug=True)

    prompt = json.dumps(client.requests[0], default=lambda value: value.__dict__, ensure_ascii=False)
    trace = response["search_trace"]
    assert "讨论生产环境部署方案" in prompt
    assert trace["context_event_count"] == 1
    assert trace["context_outcome"] == "ok"
    assert len(trace["context_hash"]) == 64
    assert "讨论生产环境部署方案" not in json.dumps(trace, ensure_ascii=False)


def test_missing_session_and_context_read_failure_fall_back(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "fallback.db")
    client = _CapturingClient()
    settings = replace(Settings(), query_expansion_mode="auto", query_context_mode="coreference")
    with database.connect() as connection:
        monkeypatch.setattr("hl_mem.application.recall.hybrid_claims", lambda *args, **kwargs: [])
        service = RecallService(connection, FakeEmbedder(4), settings=settings, query_expander=QueryExpander(client))
        service.recall("之前讨论的那个方案")
        service.recall("之前讨论的那个方案", session_id="unknown")
        monkeypatch.setattr(
            EventRepository, "get_recent_events", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError())
        )
        service.recall("之前讨论的那个方案", session_id="broken")

    assert client.requests == []


def test_context_mode_off_does_not_read_events(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "off.db")
    client = _CapturingClient()
    settings = replace(Settings(), query_expansion_mode="auto", query_context_mode="off")
    with database.connect() as connection:
        monkeypatch.setattr("hl_mem.application.recall.hybrid_claims", lambda *args, **kwargs: [])
        monkeypatch.setattr(
            EventRepository,
            "get_recent_events",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not read context")),
        )
        RecallService(
            connection,
            FakeEmbedder(4),
            settings=settings,
            query_expander=QueryExpander(client),
        ).recall("之前讨论的那个方案", session_id="session-1")

    assert len(client.requests) == 1


def test_settings_query_context_defaults() -> None:
    settings = Settings()

    assert settings.query_expansion_mode == "auto"
    assert settings.query_context_mode == "off"
    assert settings.query_context_max_events == 5
    assert settings.query_context_token_budget == 256
    assert settings.snapshot()["query_context_mode"] == "off"
