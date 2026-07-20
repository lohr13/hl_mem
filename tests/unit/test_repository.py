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
