from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from hl_mem.api.schemas import EventInput
from hl_mem.application.ingest import IngestService
from hl_mem.errors import ConflictError
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


def test_event_repository_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "unit.db")
    connection = database.open()
    event = {
        "id": "event-1",
        "idempotency_key": "same",
        "event_type": "message",
        "actor_type": "user",
        "content_json": '{"text":"你好"}',
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    repository = EventRepository(connection)
    assert repository.insert_event(event) is True
    assert repository.insert_event({**event, "id": "event-2"}) is False
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    database.close()


def test_event_repository_preserves_provenance_and_defaults_legacy_callers(tmp_path) -> None:
    database = Database(tmp_path / "provenance.db")
    repository = EventRepository(database.open())
    now = datetime.now(timezone.utc).isoformat()
    base = {
        "event_type": "message",
        "actor_type": "user",
        "content_json": "{}",
        "occurred_at": now,
        "recorded_at": now,
    }

    repository.insert_event({**base, "id": "known", "origin_class": "external", "session_kind": "cron"})
    repository.insert_event({**base, "id": "legacy"})

    assert (repository.get_event("known") or {})["origin_class"] == "external"
    assert (repository.get_event("known") or {})["session_kind"] == "cron"
    assert (repository.get_event("legacy") or {})["origin_class"] == "unknown"
    assert (repository.get_event("legacy") or {})["session_kind"] == "unknown"
    database.close()


def test_event_input_validates_provenance_before_storage() -> None:
    with pytest.raises(PydanticValidationError):
        EventInput(content="test", origin_class="invented")
    with pytest.raises(PydanticValidationError):
        EventInput(content="test", session_kind="invented")


def test_event_provenance_participates_in_idempotent_payload(tmp_path) -> None:
    database = Database(tmp_path / "idempotent-provenance.db")
    service = IngestService(database.open())
    event = EventInput(
        content="remember provenance",
        origin_class="direct_user",
        session_kind="interactive",
    ).model_dump(exclude={"namespace", "tenant_id"})

    service.ingest_event(event, idempotency_key="provenance-key")
    with pytest.raises(ConflictError, match="different event payload"):
        service.ingest_event(
            {**event, "origin_class": "external"},
            idempotency_key="provenance-key",
        )
    database.close()


def test_get_recent_events_uses_session_time_and_id_boundary(tmp_path) -> None:
    database = Database(tmp_path / "recent.db")
    repository = EventRepository(database.open())
    for event_id, namespace, session_id, occurred_at in (
        ("a", "default", "session-1", "2026-07-21T10:00:00+00:00"),
        ("b", "default", "session-1", "2026-07-21T11:00:00+00:00"),
        ("c", "default", "session-1", "2026-07-21T11:00:00+00:00"),
        ("z", "default", "session-2", "2026-07-21T10:30:00+00:00"),
        ("x", "other", "session-1", "2026-07-21T10:45:00+00:00"),
    ):
        repository.insert_event(
            {
                "id": event_id,
                "tenant_id": namespace,
                "session_id": session_id,
                "event_type": "message",
                "actor_type": "user",
                "content_json": "{}",
                "occurred_at": occurred_at,
                "recorded_at": occurred_at,
            }
        )
    recent = repository.get_recent_events(
        "default",
        "session-1",
        {"id": "c", "occurred_at": "2026-07-21T11:00:00+00:00"},
        2,
    )
    assert [event["id"] for event in recent] == ["b", "a"]
    database.close()


def test_get_recent_events_optionally_filters_user_id(tmp_path) -> None:
    """传入 user_id 时只返回该用户的会话事件。"""
    database = Database(tmp_path / "recent-user.db")
    repository = EventRepository(database.open())
    for event_id, user_id in (("a", "user-1"), ("b", "user-2")):
        repository.insert_event(
            {
                "id": event_id,
                "tenant_id": "default",
                "user_id": user_id,
                "session_id": "shared",
                "event_type": "message",
                "actor_type": "user",
                "content_json": "{}",
                "occurred_at": "2026-07-21T10:00:01+00:00" if event_id == "b" else "2026-07-21T10:00:00+00:00",
                "recorded_at": "2026-07-21T10:00:00+00:00",
            }
        )

    recent = repository.get_recent_events(
        "default",
        "shared",
        {"id": "z", "occurred_at": "2026-07-22T00:00:00+00:00"},
        10,
        user_id="user-1",
    )

    assert [event["id"] for event in recent] == ["a"]
    database.close()


def test_database_path_defaults_to_var_and_allows_settings_override(tmp_path) -> None:
    assert Path(Database().path).as_posix().endswith("var/hl_mem.db")

    configured = tmp_path / "configured.db"
    assert Path(Database(settings=Settings(database_path=str(configured))).path) == configured
