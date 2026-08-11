"""Repository integration tests for tokenized FTS v2 read/write paths."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from hl_mem.recall.lexicalizer import prepare_fts_document
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository


@pytest.fixture
def connection(tmp_path) -> Iterator[sqlite3.Connection]:
    database = Database(tmp_path / "tokenized-fts-repository.db")
    opened = database.open()
    yield opened
    database.close()


def _claim(claim_id: str, value: str, *, topic_tags_json: str = "[]") -> dict[str, object]:
    return {
        "id": claim_id,
        "predicate": "描述",
        "value": value,
        "topic_tags_json": topic_tags_json,
        "status": "active",
        "valid_from": "2026-01-01T00:00:00+00:00",
        "recorded_from": "2026-01-01T00:00:00+00:00",
    }


def _event(event_id: str, text: str) -> dict[str, object]:
    return {
        "id": event_id,
        "event_type": "message",
        "actor_type": "user",
        "content": {"text": text, "ignored": "整段 JSON 不应作为检索文档"},
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "recorded_at": "2026-01-01T00:00:00+00:00",
    }


def test_insert_claim_writes_both_v2_indexes_in_caller_transaction(connection) -> None:
    repository = ClaimRepository(connection)

    assert repository.insert_claim(
        _claim(
            "claim-v2",
            "提取任务使用 qwen3.7-plus 模型",
            topic_tags_json='["config.model","architecture","config.model"]',
        ),
        commit=False,
    )
    row = connection.execute("SELECT rowid,index_text FROM claims WHERE id='claim-v2'").fetchone()

    assert connection.execute("SELECT terms FROM claims_fts_v2 WHERE rowid=?", (row[0],)).fetchone()[
        0
    ] == prepare_fts_document(row[1])
    assert (
        connection.execute("SELECT tags_text FROM claims_tags_fts_v2 WHERE rowid=?", (row[0],)).fetchone()[0]
        == "config.model architecture"
    )

    connection.rollback()
    assert connection.execute("SELECT 1 FROM claims WHERE id='claim-v2'").fetchone() is None
    assert connection.execute("SELECT 1 FROM claims_fts_v2 WHERE rowid=?", (row[0],)).fetchone() is None
    assert connection.execute("SELECT 1 FROM claims_tags_fts_v2 WHERE rowid=?", (row[0],)).fetchone() is None


def test_claim_searches_read_only_v2_indexes(connection) -> None:
    repository = ClaimRepository(connection)
    assert repository.insert_claim(
        _claim("claim-search-v2", "提取任务使用 qwen3.7-plus 模型", topic_tags_json='["config.model","architecture"]')
    )
    row = connection.execute("SELECT rowid,index_text FROM claims WHERE id='claim-search-v2'").fetchone()
    connection.execute("DELETE FROM claims_fts_v2 WHERE rowid=?", (row[0],))
    connection.execute(
        "INSERT INTO claims_fts_v2(rowid,terms) VALUES(?,?)",
        (row[0], prepare_fts_document(row[1])),
    )
    connection.execute("DELETE FROM claims_tags_fts_v2 WHERE rowid=?", (row[0],))
    connection.execute(
        "INSERT INTO claims_tags_fts_v2(rowid,tags_text) VALUES(?,?)",
        (row[0], "config.model architecture"),
    )
    connection.execute("DROP TABLE claims_fts")
    connection.execute("DROP TABLE claims_tags_fts")
    connection.commit()

    assert [claim["id"] for claim in repository.search_claims_fts("提取模型是什么")] == ["claim-search-v2"]
    assert [claim["id"] for claim in repository.search_claims_tags(["missing", "config.model"])] == ["claim-search-v2"]


def test_auto_query_matches_old_raw_and_new_stemmed_claim_indexes(connection) -> None:
    repository = ClaimRepository(connection)
    assert repository.insert_claim(_claim("old-raw", "running databases"))
    assert repository.insert_claim(_claim("new-stemmed", "runs database"))
    old_rowid = connection.execute("SELECT rowid FROM claims WHERE id='old-raw'").fetchone()[0]
    connection.execute("UPDATE claims_fts_v2 SET terms='running databases' WHERE rowid=?", (old_rowid,))
    connection.commit()

    matches = repository.search_claims_fts("running databases")

    assert {claim["id"] for claim in matches} == {"old-raw", "new-stemmed"}


def test_insert_event_writes_v2_index_in_caller_transaction(connection) -> None:
    repository = EventRepository(connection)

    assert repository.insert_event(_event("event-v2", "中文事件使用 GPU 模型"), commit=False)
    row = connection.execute("SELECT rowid FROM events WHERE id='event-v2'").fetchone()

    assert connection.execute("SELECT terms FROM events_fts_v2 WHERE rowid=?", (row[0],)).fetchone()[
        0
    ] == prepare_fts_document("中文事件使用 GPU 模型")

    connection.rollback()
    assert connection.execute("SELECT 1 FROM events WHERE id='event-v2'").fetchone() is None
    assert connection.execute("SELECT 1 FROM events_fts_v2 WHERE rowid=?", (row[0],)).fetchone() is None


def test_event_search_reads_only_v2_index(connection) -> None:
    repository = EventRepository(connection)
    assert repository.insert_event(_event("event-search-v2", "中文事件使用 GPU 模型"))
    row = connection.execute("SELECT rowid FROM events WHERE id='event-search-v2'").fetchone()
    connection.execute("DELETE FROM events_fts_v2 WHERE rowid=?", (row[0],))
    connection.execute(
        "INSERT INTO events_fts_v2(rowid,terms) VALUES(?,?)",
        (row[0], prepare_fts_document("中文事件使用 GPU 模型")),
    )
    connection.execute("DROP TABLE events_fts")
    connection.commit()

    assert [event["id"] for event in repository.search_events_fts("中文 GPU")] == ["event-search-v2"]
