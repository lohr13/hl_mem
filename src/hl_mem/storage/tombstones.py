"""Independent, fail-loud tombstone ledger for physical memory deletion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

TOMBSTONE_SCHEMA_VERSION = 1


class TombstoneLedgerError(RuntimeError):
    """Base error for tombstone ledger validation and writes."""


class TombstoneLedgerVersionError(TombstoneLedgerError):
    """The sidecar schema is not the version understood by this runtime."""


class TombstoneLedgerConflictError(TombstoneLedgerError):
    """An existing deletion identity carries a different closure contract."""


class TombstoneLedgerWriteError(TombstoneLedgerError):
    """A durable tombstone could not be written."""


@dataclass(frozen=True)
class TombstoneEntry:
    identity_hash: str
    claim_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    closure_scope: tuple[str, ...]
    created_at: str


def default_tombstone_ledger_path(database_path: str | Path) -> Path:
    """Derive the sidecar path without adding a user-facing setting."""
    database = Path(database_path).expanduser().resolve()
    return database.with_suffix(f"{database.suffix}.tombstones.db")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalized(values: Iterable[str], *, label: str, allow_empty: bool = False) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip() for value in values if str(value).strip()}))
    if not normalized and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    return normalized


class TombstoneLedger:
    """Versioned SQLite sidecar containing opaque deletion identities, never claim text."""

    def __init__(self, path: str | Path, *, create: bool = True) -> None:
        self.path = Path(path).expanduser().resolve()
        if not create and not self.path.is_file():
            raise FileNotFoundError(f"tombstone ledger does not exist: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(create=create)

    @property
    def ledger_id(self) -> str:
        connection = self._connect()
        try:
            return self._validate(connection)
        finally:
            connection.close()

    def validate(self) -> None:
        connection = self._connect()
        try:
            self._validate(connection)
        finally:
            connection.close()

    def count(self) -> int:
        """Return the number of durable deletion identities."""
        connection = self._connect()
        try:
            self._validate(connection)
            return int(connection.execute("SELECT count(*) FROM tombstones").fetchone()[0])
        finally:
            connection.close()

    def find_by_claim_id(self, claim_id: str) -> TombstoneEntry | None:
        """Find a prior deletion without exposing or storing claim content."""
        normalized_claim_id = str(claim_id).strip()
        if not normalized_claim_id:
            return None
        connection = self._connect()
        try:
            self._validate(connection)
            row = connection.execute(
                "SELECT t.identity_hash,t.claim_ids_json,t.event_ids_json,"
                "t.closure_scope_json,t.created_at FROM tombstones AS t "
                "WHERE EXISTS ("
                "SELECT 1 FROM json_each(t.claim_ids_json) WHERE value=?"
                ") ORDER BY t.created_at DESC LIMIT 1",
                (normalized_claim_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return TombstoneEntry(
            identity_hash=str(row[0]),
            claim_ids=tuple(json.loads(row[1])),
            event_ids=tuple(json.loads(row[2])),
            closure_scope=tuple(json.loads(row[3])),
            created_at=str(row[4]),
        )

    def find_by_identity_hash(self, identity_hash: str) -> TombstoneEntry | None:
        """Load one durable tombstone by its opaque identity."""
        normalized_hash = str(identity_hash).strip()
        if not normalized_hash:
            return None
        connection = self._connect()
        try:
            self._validate(connection)
            row = connection.execute(
                "SELECT identity_hash,claim_ids_json,event_ids_json,"
                "closure_scope_json,created_at FROM tombstones WHERE identity_hash=?",
                (normalized_hash,),
            ).fetchone()
        finally:
            connection.close()
        return self._entry_from_row(row)

    def entries(self) -> tuple[TombstoneEntry, ...]:
        """Return the append-only replay stream in deterministic order."""
        connection = self._connect()
        try:
            self._validate(connection)
            rows = connection.execute(
                "SELECT identity_hash,claim_ids_json,event_ids_json,"
                "closure_scope_json,created_at FROM tombstones "
                "ORDER BY created_at,identity_hash"
            ).fetchall()
        finally:
            connection.close()
        entries: list[TombstoneEntry] = []
        for row in rows:
            entry = self._entry_from_row(row)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    @staticmethod
    def _entry_from_row(row: sqlite3.Row | tuple[object, ...] | None) -> TombstoneEntry | None:
        if row is None:
            return None
        return TombstoneEntry(
            identity_hash=str(row[0]),
            claim_ids=tuple(json.loads(str(row[1]))),
            event_ids=tuple(json.loads(str(row[2]))),
            closure_scope=tuple(json.loads(str(row[3]))),
            created_at=str(row[4]),
        )

    def record_deletion(
        self,
        *,
        claim_ids: Iterable[str],
        event_ids: Iterable[str],
        closure_scope: Iterable[str],
    ) -> TombstoneEntry:
        claims = _normalized(claim_ids, label="claim_ids")
        events = _normalized(event_ids, label="event_ids", allow_empty=True)
        scope = _normalized(closure_scope, label="closure_scope")
        identity_payload = {"claim_ids": claims, "event_ids": events}
        identity_hash = hashlib.sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validate(connection)
            existing = connection.execute(
                "SELECT claim_ids_json,event_ids_json,closure_scope_json,created_at "
                "FROM tombstones WHERE identity_hash=?",
                (identity_hash,),
            ).fetchone()
            if existing is not None:
                stored_claims = tuple(json.loads(existing[0]))
                stored_events = tuple(json.loads(existing[1]))
                stored_scope = tuple(json.loads(existing[2]))
                if stored_claims != claims or stored_events != events:
                    raise TombstoneLedgerConflictError("deletion identity hash payload does not match")
                if stored_scope != scope:
                    raise TombstoneLedgerConflictError("existing deletion identity has a different closure scope")
                connection.commit()
                return TombstoneEntry(identity_hash, claims, events, scope, str(existing[3]))

            created_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO tombstones("
                "identity_hash,claim_ids_json,event_ids_json,closure_scope_json,created_at"
                ") VALUES (?,?,?,?,?)",
                (
                    identity_hash,
                    _canonical_json(claims),
                    _canonical_json(events),
                    _canonical_json(scope),
                    created_at,
                ),
            )
            connection.commit()
            return TombstoneEntry(identity_hash, claims, events, scope, created_at)
        except TombstoneLedgerError:
            if connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise TombstoneLedgerWriteError(f"tombstone ledger write failed: {error}") from error
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self, *, create: bool) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstone_ledger_meta'"
            ).fetchone()
            if has_meta is None:
                if not create:
                    raise TombstoneLedgerVersionError("tombstone ledger schema is missing")
                connection.execute(
                    "CREATE TABLE tombstone_ledger_meta ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton=1),"
                    "schema_version INTEGER NOT NULL,"
                    "ledger_id TEXT NOT NULL UNIQUE,"
                    "created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE tombstones ("
                    "identity_hash TEXT PRIMARY KEY,"
                    "claim_ids_json TEXT NOT NULL,"
                    "event_ids_json TEXT NOT NULL,"
                    "closure_scope_json TEXT NOT NULL,"
                    "created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO tombstone_ledger_meta(singleton,schema_version,ledger_id,created_at) "
                    "VALUES (1,?,?,?)",
                    (
                        TOMBSTONE_SCHEMA_VERSION,
                        uuid.uuid4().hex,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.execute(f"PRAGMA user_version={TOMBSTONE_SCHEMA_VERSION}")
            self._validate(connection)
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> str:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        try:
            rows = connection.execute(
                "SELECT schema_version,ledger_id FROM tombstone_ledger_meta WHERE singleton=1"
            ).fetchall()
            has_tombstones = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstones'"
            ).fetchone()
        except sqlite3.Error as error:
            raise TombstoneLedgerVersionError(f"tombstone ledger schema is invalid: {error}") from error
        if len(rows) != 1 or has_tombstones is None:
            raise TombstoneLedgerVersionError("tombstone ledger metadata is missing or ambiguous")
        schema_version = int(rows[0][0])
        if schema_version != TOMBSTONE_SCHEMA_VERSION or user_version != TOMBSTONE_SCHEMA_VERSION:
            raise TombstoneLedgerVersionError(
                "unsupported tombstone ledger version: "
                f"metadata={schema_version}, user_version={user_version}, "
                f"expected={TOMBSTONE_SCHEMA_VERSION}"
            )
        ledger_id = str(rows[0][1]).strip()
        if not ledger_id:
            raise TombstoneLedgerVersionError("tombstone ledger identity is empty")
        return ledger_id
