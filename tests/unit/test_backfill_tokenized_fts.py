"""测试 tokenized FTS v2 的全量单事务回填。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hl_mem.storage.database import Database
from hl_mem.workers.backfill_tokenized_fts import backfill_tokenized_fts


@pytest.fixture
def migrated_database(tmp_path: Path) -> Iterator[tuple[Database, sqlite3.Connection]]:
    database = Database(tmp_path / "backfill-tokenized-fts.db")
    connection = database.open()
    yield database, connection
    database.close()


def _insert_claims(connection: sqlite3.Connection, count: int) -> None:
    connection.executemany(
        "INSERT INTO claims(id,recorded_from,status,index_text,topic_tags_json) VALUES(?,?,?,?,?)",
        (
            (
                f"claim-{index}",
                "2026-08-01T00:00:00+00:00",
                "active",
                "" if index == 0 else f"memory system {index}",
                json.dumps(["memory", "python", "memory", index, None]),
            )
            for index in range(count)
        ),
    )
    connection.commit()


def _insert_events(connection: sqlite3.Connection, count: int) -> None:
    connection.executemany(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) " "VALUES(?,?,?,?,?,?)",
        (
            (
                f"event-{index}",
                "message",
                "user",
                json.dumps({"text": f"event memory {index}"}),
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            )
            for index in range(count)
        ),
    )
    connection.commit()


def test_claims_backfill_rebuilds_all_rows_including_empty_text_and_is_idempotent(
    migrated_database: tuple[Database, sqlite3.Connection],
) -> None:
    """漏回填、跳过空文本或未先清空旧索引都会失败。"""
    _, connection = migrated_database
    _insert_claims(connection, 1_376)

    assert backfill_tokenized_fts(connection, "claims") == 1_376
    assert connection.execute("SELECT count(*) FROM claims_fts_v2").fetchone()[0] == 1_376
    empty_terms = connection.execute(
        "SELECT terms FROM claims_fts_v2 WHERE rowid=(SELECT rowid FROM claims WHERE id='claim-0')"
    ).fetchone()[0]
    assert empty_terms == ""

    connection.execute("INSERT INTO claims_fts_v2(rowid,terms) VALUES(999999,'stale')")
    connection.commit()
    assert backfill_tokenized_fts(connection, "claims") == 1_376
    assert connection.execute("SELECT count(*) FROM claims_fts_v2").fetchone()[0] == 1_376
    assert connection.execute("SELECT 1 FROM claims_fts_v2 WHERE rowid=999999").fetchone() is None


def test_events_backfill_rebuilds_all_rows(
    migrated_database: tuple[Database, sqlite3.Connection],
) -> None:
    """events channel 必须从 content_json.text 回填全部 11,941 行。"""
    _, connection = migrated_database
    _insert_events(connection, 11_941)

    assert backfill_tokenized_fts(connection, "events") == 11_941
    assert connection.execute("SELECT count(*) FROM events_fts_v2").fetchone()[0] == 11_941
    terms = connection.execute(
        "SELECT terms FROM events_fts_v2 WHERE rowid=(SELECT rowid FROM events WHERE id='event-42')"
    ).fetchone()[0]
    assert terms == "event memory memori 42"


def test_events_backfill_indexes_non_object_or_non_string_text_as_empty_document(
    migrated_database: tuple[Database, sqlite3.Connection],
) -> None:
    """历史 event 的合法 JSON 不是对象或 text 不是字符串时不得中断全量升级。"""
    _, connection = migrated_database
    connection.executemany(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) VALUES(?,?,?,?,?,?)",
        (
            (
                event_id,
                "message",
                "user",
                content_json,
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            )
            for event_id, content_json in (
                ("event-scalar", '"text"'),
                ("event-list", "[]"),
                ("event-non-string-text", '{"text":[]}'),
            )
        ),
    )
    connection.commit()

    assert backfill_tokenized_fts(connection, "events") == 3
    assert [
        row[0]
        for row in connection.execute(
            "SELECT events_fts_v2.terms FROM events_fts_v2 "
            "JOIN events ON events.rowid=events_fts_v2.rowid ORDER BY events.id"
        )
    ] == ["", "", ""]


def test_tags_backfill_deduplicates_string_tags_and_rebuilds_all_rows(
    migrated_database: tuple[Database, sqlite3.Connection],
) -> None:
    """tags channel 必须保序去重并忽略非字符串 JSON 元素。"""
    _, connection = migrated_database
    _insert_claims(connection, 1_376)

    assert backfill_tokenized_fts(connection, "tags") == 1_376
    assert connection.execute("SELECT count(*) FROM claims_tags_fts_v2").fetchone()[0] == 1_376
    tags_text = connection.execute(
        "SELECT tags_text FROM claims_tags_fts_v2 " "WHERE rowid=(SELECT rowid FROM claims WHERE id='claim-42')"
    ).fetchone()[0]
    assert tags_text == "memory python"


def test_backfill_rolls_back_delete_and_partial_rebuild_on_failure(
    migrated_database: tuple[Database, sqlite3.Connection],
) -> None:
    """任一源行无法解析时，DELETE 和此前 INSERT 必须一起回滚。"""
    _, connection = migrated_database
    connection.execute("INSERT INTO events_fts_v2(rowid,terms) VALUES(999999,'sentinel')")
    connection.executemany(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) " "VALUES(?,?,?,?,?,?)",
        (
            (
                "event-good",
                "message",
                "user",
                '{"text":"memory system"}',
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
            (
                "event-bad",
                "message",
                "user",
                "{",
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
            ),
        ),
    )
    connection.commit()

    with pytest.raises(json.JSONDecodeError):
        backfill_tokenized_fts(connection, "events")

    assert [tuple(row) for row in connection.execute("SELECT rowid,terms FROM events_fts_v2")] == [(999999, "sentinel")]
