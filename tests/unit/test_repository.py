from datetime import datetime, timezone

from hl_mem.storage.database import Database
from hl_mem.storage.repository import EventRepository


def test_event_repository_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "unit.db")
    connection = database.open()
    event = {
        "id": "event-1", "idempotency_key": "same", "event_type": "message",
        "actor_type": "user", "content_json": '{"text":"你好"}',
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    repository = EventRepository(connection)
    assert repository.insert_event(event) is True
    assert repository.insert_event({**event, "id": "event-2"}) is False
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    database.close()


def test_get_recent_events_uses_session_time_and_id_boundary(tmp_path) -> None:
    database = Database(tmp_path / "recent.db")
    repository = EventRepository(database.open())
    for event_id, session_id, occurred_at in (
        ("a", "session-1", "2026-07-21T10:00:00+00:00"),
        ("b", "session-1", "2026-07-21T11:00:00+00:00"),
        ("c", "session-1", "2026-07-21T11:00:00+00:00"),
        ("z", "session-2", "2026-07-21T10:30:00+00:00"),
    ):
        repository.insert_event({
            "id": event_id, "session_id": session_id, "event_type": "message",
            "actor_type": "user", "content_json": '{}', "occurred_at": occurred_at,
            "recorded_at": occurred_at,
        })
    recent = repository.get_recent_events(
        "session-1", {"id": "c", "occurred_at": "2026-07-21T11:00:00+00:00"}, 2
    )
    assert [event["id"] for event in recent] == ["b", "a"]
    database.close()
