from __future__ import annotations

import json
import sqlite3

import pytest

from hl_mem.storage.tombstones import (
    TOMBSTONE_SCHEMA_VERSION,
    TombstoneLedger,
    TombstoneLedgerConflictError,
    TombstoneLedgerVersionError,
    TombstoneLedgerWriteError,
)


def test_record_deletion_is_idempotent_and_contains_only_deletion_identity(tmp_path) -> None:
    path = tmp_path / "memory.db.tombstones.db"
    ledger = TombstoneLedger(path)

    first = ledger.record_deletion(
        claim_ids=["claim-b", "claim-a", "claim-a"],
        event_ids=["event-a"],
        closure_scope=["claim", "evidence", "event"],
    )
    second = ledger.record_deletion(
        claim_ids=["claim-a", "claim-b"],
        event_ids=["event-a", "event-a"],
        closure_scope=["claim", "evidence", "event"],
    )

    assert second == first
    assert first.claim_ids == ("claim-a", "claim-b")
    assert first.event_ids == ("event-a",)
    assert len(first.identity_hash) == 64
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == TOMBSTONE_SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM tombstones").fetchone()[0] == 1
        row = connection.execute("SELECT claim_ids_json,event_ids_json,closure_scope_json FROM tombstones").fetchone()
        assert json.loads(row[0]) == ["claim-a", "claim-b"]
        assert json.loads(row[1]) == ["event-a"]
        assert json.loads(row[2]) == ["claim", "event", "evidence"]
        columns = {item[1] for item in connection.execute("PRAGMA table_info(tombstones)")}
        assert columns == {
            "identity_hash",
            "claim_ids_json",
            "event_ids_json",
            "closure_scope_json",
            "created_at",
        }


def test_record_deletion_rejects_changed_scope_for_existing_identity(tmp_path) -> None:
    ledger = TombstoneLedger(tmp_path / "ledger.db")
    ledger.record_deletion(
        claim_ids=["claim-a"],
        event_ids=["event-a"],
        closure_scope=["claim", "event"],
    )

    with pytest.raises(TombstoneLedgerConflictError, match="closure scope"):
        ledger.record_deletion(
            claim_ids=["claim-a"],
            event_ids=["event-a"],
            closure_scope=["claim", "event", "relation"],
        )


def test_open_rejects_unknown_schema_version(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = TombstoneLedger(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE tombstone_ledger_meta SET schema_version=999")
        connection.execute("PRAGMA user_version=999")

    with pytest.raises(TombstoneLedgerVersionError, match="999"):
        ledger.validate()


def test_record_deletion_surfaces_sqlite_write_failure_without_partial_row(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    ledger = TombstoneLedger(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_tombstone BEFORE INSERT ON tombstones "
            "BEGIN SELECT RAISE(ABORT, 'forced ledger failure'); END"
        )

    with pytest.raises(TombstoneLedgerWriteError, match="forced ledger failure"):
        ledger.record_deletion(
            claim_ids=["claim-a"],
            event_ids=[],
            closure_scope=["claim"],
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM tombstones").fetchone()[0] == 0
