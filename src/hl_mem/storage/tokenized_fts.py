"""Tokenized FTS v2 document extraction, rebuilds, and startup integrity."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from hl_mem.recall.lexicalizer import prepare_fts_document

BackfillChannel = Literal["claims", "events", "tags"]
CHANNELS: tuple[BackfillChannel, ...] = ("claims", "events", "tags")


def event_text_for_fts(content_json: str | bytes | bytearray) -> str:
    """Extract the searchable text field from a stored event payload."""
    content = json.loads(content_json)
    if not isinstance(content, dict):
        return ""
    text = content.get("text", "")
    return text if isinstance(text, str) else ""


def _backfill_claims(connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM claims_fts_v2")
    rows = connection.execute("SELECT rowid, index_text FROM claims").fetchall()
    connection.executemany(
        "INSERT INTO claims_fts_v2(rowid, terms) VALUES(?, ?)",
        ((row[0], prepare_fts_document(row[1] or "")) for row in rows),
    )
    return len(rows)


def _backfill_events(connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM events_fts_v2")
    rows = connection.execute("SELECT rowid, content_json FROM events").fetchall()
    connection.executemany(
        "INSERT INTO events_fts_v2(rowid, terms) VALUES(?, ?)",
        ((row[0], prepare_fts_document(event_text_for_fts(row[1]))) for row in rows),
    )
    return len(rows)


def _backfill_tags(connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM claims_tags_fts_v2")
    rows = connection.execute("SELECT rowid, topic_tags_json FROM claims").fetchall()

    def documents() -> list[tuple[int, str]]:
        prepared: list[tuple[int, str]] = []
        for row in rows:
            raw_tags = json.loads(row[1] or "[]")
            if not isinstance(raw_tags, list):
                raise ValueError(f"topic_tags_json for claim rowid {row[0]} must be a JSON array")
            unique_tags = dict.fromkeys(tag for tag in raw_tags if isinstance(tag, str))
            prepared.append((row[0], " ".join(unique_tags)))
        return prepared

    connection.executemany("INSERT INTO claims_tags_fts_v2(rowid, tags_text) VALUES(?, ?)", documents())
    return len(rows)


_BUILDERS = {
    "claims": _backfill_claims,
    "events": _backfill_events,
    "tags": _backfill_tags,
}

_CHANNEL_TABLES = {
    "claims": ("claims", "claims_fts_v2"),
    "events": ("events", "events_fts_v2"),
    "tags": ("claims", "claims_tags_fts_v2"),
}


def _rowids_match(connection: sqlite3.Connection, source_table: str, index_table: str) -> bool:
    missing = connection.execute(
        f"SELECT 1 FROM (SELECT rowid FROM {source_table} EXCEPT SELECT rowid FROM {index_table}) LIMIT 1"
    ).fetchone()
    extra = connection.execute(
        f"SELECT 1 FROM (SELECT rowid FROM {index_table} EXCEPT SELECT rowid FROM {source_table}) LIMIT 1"
    ).fetchone()
    return missing is None and extra is None


def tokenized_fts_v2_is_complete(connection: sqlite3.Connection) -> bool:
    """Return whether every v2 channel covers exactly its source rowids."""
    return all(_rowids_match(connection, *_CHANNEL_TABLES[channel]) for channel in CHANNELS)


def _run_atomic_rebuild(
    connection: sqlite3.Connection,
    channels: tuple[BackfillChannel, ...],
) -> dict[BackfillChannel, int]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        counts = {channel: _BUILDERS[channel](connection) for channel in channels}
        if not all(_rowids_match(connection, *_CHANNEL_TABLES[channel]) for channel in channels):
            raise RuntimeError("tokenized FTS v2 rowid integrity check failed after rebuild")
        connection.commit()
        return counts
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def backfill_tokenized_fts(connection: sqlite3.Connection, channel: BackfillChannel) -> int:
    """Delete and rebuild one v2 channel in a single immediate transaction."""
    if channel not in CHANNELS:
        raise ValueError(f"unsupported tokenized FTS channel: {channel}")
    return _run_atomic_rebuild(connection, (channel,))[channel]


def ensure_tokenized_fts_v2(connection: sqlite3.Connection) -> bool:
    """Atomically populate all v2 channels before v2-only reads are exposed."""
    if tokenized_fts_v2_is_complete(connection):
        return False
    _run_atomic_rebuild(connection, CHANNELS)
    return True
