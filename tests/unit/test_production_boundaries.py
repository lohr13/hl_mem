import json

import pytest

from hl_mem.security.retention import enforce_event_quota, purge_retained_events
from hl_mem.storage.backup import backup_database, restore_database
from hl_mem.storage.database import Database
from hl_mem.storage.postgres import PostgresDatabase


def _event(connection, event_id: str, tenant: str, recorded_at: str) -> None:
    connection.execute(
        "INSERT INTO events(id,tenant_id,event_type,actor_type,content_json,occurred_at,recorded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (event_id, tenant, "message", "user", json.dumps({"text": event_id}), recorded_at, recorded_at),
    )
    connection.commit()


def test_backup_restore_validates_sha256_manifest(tmp_path) -> None:
    source = tmp_path / "source.db"
    _event(Database(source).open(), "e1", "t1", "2026-01-01T00:00:00Z")
    backup = tmp_path / "backup.db"
    manifest = backup_database(source, backup)
    target = tmp_path / "target.db"
    restore_database(backup, manifest, target)
    assert Database(target).open().execute("SELECT count(*) FROM events").fetchone()[0] == 1

    backup.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        restore_database(backup, manifest, target)


def test_quota_and_retention_are_tenant_scoped(tmp_path) -> None:
    connection = Database(tmp_path / "retention.db").open()
    _event(connection, "a1", "a", "2025-01-01T00:00:00Z")
    _event(connection, "a2", "a", "2026-01-01T00:00:00Z")
    _event(connection, "b1", "b", "2025-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="quota"):
        enforce_event_quota(connection, "a", 2)
    assert purge_retained_events(connection, "a", "2025-06-01T00:00:00Z") == 1
    assert connection.execute("SELECT count(*) FROM events WHERE tenant_id='b'").fetchone()[0] == 1


def test_postgres_adapter_is_optional_and_reports_missing_driver() -> None:
    adapter = PostgresDatabase("postgresql://example.invalid/db")
    with pytest.raises(RuntimeError, match="psycopg"):
        adapter.open()
