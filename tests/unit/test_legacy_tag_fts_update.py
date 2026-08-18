from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.ingest.extractors import ExtractedClaim
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.migrations.backfill_claim_slots_v1 import backfill_claim_slots_v1
from scripts.reextract_claims import update_existing_claim


def _insert_unclassified_claim(database: Database) -> None:
    connection = database.open()
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,subject_entity_id,predicate,canonical_attribute,canonical_slot,"
        "topic_tags_json,qualifiers_json,recorded_from,status"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "claim",
            "default",
            "user",
            "偏好",
            "preference.ui_theme",
            None,
            '["legacy"]',
            "{}",
            "2026-08-18T00:00:00+00:00",
            "active",
        ),
    )
    connection.commit()


def _install_failing_legacy_tag_update_trigger(database: Database) -> str:
    connection = database.open()
    connection.execute("DROP TRIGGER IF EXISTS claims_tags_au")
    connection.execute(
        "CREATE TRIGGER claims_tags_au AFTER UPDATE OF topic_tags_json ON claims BEGIN "
        "INSERT INTO claims_tags_fts(claims_tags_fts,rowid,tags_text) "
        "VALUES(CASE WHEN old.topic_tags_json='[\"legacy\"]' THEN 'broken-delete' ELSE 'delete' END,"
        "old.rowid,COALESCE(old.topic_tags_json,'')); "
        "INSERT INTO claims_tags_fts(rowid,tags_text) "
        "VALUES(new.rowid,COALESCE(new.topic_tags_json,'')); "
        "END"
    )
    connection.commit()
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='claims_tags_au'").fetchone()
    assert row is not None
    return str(row["sql"])


def test_slot_backfill_keeps_normal_tag_trigger_behavior(tmp_path: Path) -> None:
    database = Database(tmp_path / "normal-tag-update.db")
    try:
        _insert_unclassified_claim(database)

        stats = backfill_claim_slots_v1(database.open(), apply=True, force=True)

        claim = ClaimRepository(database.open()).get_claim("claim")
        assert stats.applied == 1
        assert claim is not None and claim["topic_tags"] == ["preference"]
    finally:
        database.close()


def test_slot_backfill_recovers_legacy_tag_projection_and_restores_trigger(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-tag-update.db")
    try:
        _insert_unclassified_claim(database)
        trigger_sql = _install_failing_legacy_tag_update_trigger(database)

        stats = backfill_claim_slots_v1(database.open(), apply=True, force=True)

        connection = database.open()
        claim = ClaimRepository(connection).get_claim("claim")
        restored = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='claims_tags_au'"
        ).fetchone()
        assert stats.applied == 1
        assert claim is not None and claim["topic_tags"] == ["preference"]
        assert restored is not None and restored["sql"] == trigger_sql

        connection.execute("UPDATE claims SET topic_tags_json=? WHERE id=?", ('["restored"]', "claim"))
        connection.commit()
        indexed = connection.execute(
            "SELECT c.id FROM claims_tags_fts f JOIN claims c ON c.rowid=f.rowid " "WHERE claims_tags_fts MATCH ?",
            ('"restored"',),
        ).fetchone()
        assert indexed is not None and indexed["id"] == "claim"
    finally:
        database.close()


def test_slot_backfill_restores_legacy_tag_trigger_when_projection_cleanup_fails(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-tag-update-failure.db")
    try:
        _insert_unclassified_claim(database)
        connection = database.open()
        row = connection.execute("SELECT rowid,topic_tags_json FROM claims WHERE id='claim'").fetchone()
        assert row is not None
        connection.execute("DROP TRIGGER claims_tags_au")
        connection.execute(
            "INSERT INTO claims_tags_fts(claims_tags_fts,rowid,tags_text) VALUES('delete',?,?)",
            (row["rowid"], row["topic_tags_json"]),
        )
        connection.commit()
        trigger_sql = _install_failing_legacy_tag_update_trigger(database)

        with pytest.raises(sqlite3.DatabaseError) as error:
            backfill_claim_slots_v1(connection, apply=True, force=True)

        restored = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='claims_tags_au'"
        ).fetchone()
        claim = ClaimRepository(connection).get_claim("claim")
        assert type(error.value) is sqlite3.DatabaseError
        assert restored is not None and restored["sql"] == trigger_sql
        assert claim is not None and claim["topic_tags"] == ["legacy"]
    finally:
        database.close()


def test_reextract_claim_update_uses_legacy_tag_projection_recovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-tag-reextract.db")
    try:
        _insert_unclassified_claim(database)
        _install_failing_legacy_tag_update_trigger(database)

        updated = update_existing_claim(
            database.open(),
            "claim",
            ExtractedClaim(
                predicate="偏好",
                value="深色模式",
                topic_tags=["preference"],
            ),
            FakeEmbedder(8),
        )

        claim = ClaimRepository(database.open()).get_claim("claim")
        assert updated is True
        assert claim is not None and claim["topic_tags"] == ["preference"]
    finally:
        database.close()
