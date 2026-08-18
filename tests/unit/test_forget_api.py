from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.storage.database import Database


def _insert_claim_with_failing_legacy_tag_delete(database_path: Path) -> None:
    database = Database(database_path)
    connection = database.open()
    try:
        connection.execute(
            "INSERT INTO claims(" "id,recorded_from,status,index_text,topic_tags_json" ") VALUES(?,?,?,?,?)",
            (
                "claim-old",
                "2026-08-18T00:00:00+00:00",
                "superseded",
                "旧配置",
                '["config","deployment"]',
            ),
        )
        connection.execute("DROP TRIGGER claims_tags_ad")
        connection.execute(
            "CREATE TRIGGER claims_tags_ad AFTER DELETE ON claims BEGIN "
            "INSERT INTO claims_tags_fts(claims_tags_fts,rowid,tags_text) "
            "VALUES('broken-delete',old.rowid,COALESCE(old.topic_tags_json,'')); "
            "END"
        )
        connection.commit()
    finally:
        database.close()


def test_delete_memory_recovers_from_legacy_tag_fts_delete_failure(tmp_path: Path) -> None:
    database_path = tmp_path / "forget-existing.db"
    _insert_claim_with_failing_legacy_tag_delete(database_path)

    with TestClient(create_app(database_path), raise_server_exceptions=False) as client:
        response = client.delete("/v1/memories/claim-old")

    assert response.status_code == 200
    assert response.json()["forgotten"] is True
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT 1 FROM claims WHERE id='claim-old'").fetchone() is None
    finally:
        connection.close()


def test_delete_memory_returns_404_for_unknown_claim(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "forget-missing.db"), raise_server_exceptions=False) as client:
        response = client.delete("/v1/memories/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "memory not found"}
