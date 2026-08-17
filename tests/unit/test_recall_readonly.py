"""召回只读连接与请求内零同步写回归。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database


def test_readonly_connection_reads_wal_snapshot_and_rejects_writes(tmp_path) -> None:
    """捕获把任意 SQLite 写重新放回 recall 连接的回归。"""
    database = Database(tmp_path / "recall-readonly.db")
    writer = database.open()
    writer.execute("CREATE TABLE readonly_probe(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
    writer.execute("INSERT INTO readonly_probe VALUES ('committed','visible')")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO readonly_probe VALUES ('pending','locked')")

    try:
        with database.connect_readonly() as reader:
            assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
            assert reader.execute("SELECT value FROM readonly_probe WHERE id='committed'").fetchone()[0] == "visible"
            assert reader.execute("SELECT 1 FROM readonly_probe WHERE id='pending'").fetchone() is None
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|query_only"):
                reader.execute("INSERT INTO readonly_probe VALUES ('forbidden','write')")
    finally:
        writer.rollback()
        database.close()


def test_api_recall_returns_under_two_seconds_while_extraction_write_lock_is_held(tmp_path) -> None:
    path = tmp_path / "recall-under-write-load.db"
    settings = replace(
        Settings.for_test(),
        database_path=str(path),
        database_busy_timeout_seconds=3,
        recall_dense_enabled=False,
        resurrection_mode="off",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        seed = app.state.db.open()
        ClaimRepository(seed).insert_claim(
            {
                "id": "claim-load",
                "status": "active",
                "subject_entity_id": "user",
                "predicate": "likes",
                "value_json": '"tea"',
                "index_text": "likes tea",
                "recorded_from": "2026-08-18T00:00:00+00:00",
            }
        )
        locker = sqlite3.connect(path, timeout=3)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("UPDATE claims SET confidence=confidence WHERE id='claim-load'")
        try:
            started = time.monotonic()
            response = client.post(
                "/v1/internal/retrieval-bundles",
                json={"query": "likes tea", "limit": 1},
            )
            elapsed = time.monotonic() - started
            assert response.status_code == 200
            assert response.json()["retrieval_bundle"]["items"][0]["id"] == "claim-load"
            assert elapsed < 2.0
        finally:
            locker.rollback()
            locker.close()
