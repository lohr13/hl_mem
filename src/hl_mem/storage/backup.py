"""SQLite online backup, manifest validation, and atomic restore."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hl_mem.storage.database import register_entity_sqlite_functions
from hl_mem.storage.tombstones import (
    TOMBSTONE_SCHEMA_VERSION,
    TombstoneLedger,
    TombstoneLedgerError,
    default_tombstone_ledger_path,
)

BACKUP_FORMAT_VERSION = 2
LEGACY_BACKUP_FORMAT_VERSION = 1
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
RestoreReplay = Callable[[sqlite3.Connection, TombstoneLedger], tuple[int, int, int]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _connection(database: str | Path, *, timeout: float = 5.0, uri: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=timeout, uri=uri)
    register_entity_sqlite_functions(connection)
    return connection


def _readonly_connection(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return _connection(f"{path.as_uri()}?{query}", uri=True)


def _assert_no_sidecars(path: Path, role: str) -> None:
    sidecars = [Path(f"{path}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES]
    existing = [sidecar for sidecar in sidecars if sidecar.exists()]
    if existing:
        names = ", ".join(str(sidecar) for sidecar in existing)
        raise ValueError(
            f"{role} has SQLite sidecar files; stop all database users and " f"resolve them before continuing: {names}"
        )


def _integrity_check(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        detail = "; ".join(messages) or "no result"
        raise ValueError(f"SQLite integrity check failed: {detail}")
    return "ok"


def _temporary_path(parent: Path, filename: str) -> Path:
    descriptor, value = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=parent,
    )
    os.close(descriptor)
    return Path(value)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = _temporary_path(path.parent, path.name)
    try:
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_ledger_binding(connection: sqlite3.Connection, *, role: str) -> tuple[str, int]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deletion_ledger_state'"
    ).fetchone()
    if table is None:
        raise ValueError(f"{role} cannot prove deletion history: migration 043 is missing")
    rows = connection.execute("SELECT ledger_id,schema_version FROM deletion_ledger_state WHERE singleton=1").fetchall()
    if len(rows) != 1:
        raise ValueError(f"{role} has no unambiguous tombstone ledger identity")
    ledger_id = str(rows[0][0]).strip()
    schema_version = int(rows[0][1])
    if not ledger_id or schema_version != TOMBSTONE_SCHEMA_VERSION:
        raise ValueError(f"{role} tombstone ledger binding is invalid")
    return ledger_id, schema_version


def _ensure_backup_ledger(source: Path) -> tuple[str, int]:
    """Bind an empty ledger before the first backup so future deletes remain provable."""
    connection = _connection(source, timeout=5.0)
    ledger_path = default_tombstone_ledger_path(source)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deletion_ledger_state'"
        ).fetchone()
        if table is None:
            raise ValueError("backup source cannot prove deletion history: migration 043 is missing")
        state = connection.execute(
            "SELECT ledger_id,schema_version FROM deletion_ledger_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            try:
                ledger = TombstoneLedger(ledger_path)
            except (OSError, TombstoneLedgerError) as error:
                raise ValueError(f"backup tombstone ledger initialization failed: {error}") from error
            connection.execute(
                "INSERT INTO deletion_ledger_state(" "singleton,ledger_id,schema_version,bound_at" ") VALUES (1,?,?,?)",
                (
                    ledger.ledger_id,
                    TOMBSTONE_SCHEMA_VERSION,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        else:
            if not ledger_path.is_file():
                raise ValueError(f"backup tombstone ledger is missing: {ledger_path}")
            try:
                ledger = TombstoneLedger(ledger_path, create=False)
            except (OSError, TombstoneLedgerError) as error:
                raise ValueError(f"backup tombstone ledger is invalid: {error}") from error
            if str(state[0]) != ledger.ledger_id:
                raise ValueError("backup tombstone ledger identity mismatch")
            if int(state[1]) != TOMBSTONE_SCHEMA_VERSION:
                raise ValueError("backup tombstone ledger schema version mismatch")
        connection.commit()
        return ledger.ledger_id, TOMBSTONE_SCHEMA_VERSION
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _manifest_ledger(metadata: dict[str, Any]) -> tuple[str, int]:
    version = metadata.get("format_version")
    if version == LEGACY_BACKUP_FORMAT_VERSION:
        raise ValueError("legacy backup manifest has no tombstone ledger identity; restore is refused")
    if version != BACKUP_FORMAT_VERSION:
        raise ValueError("backup manifest version is invalid")
    raw = metadata.get("tombstone_ledger")
    if not isinstance(raw, dict):
        raise ValueError("backup manifest tombstone ledger identity is missing")
    ledger_id = raw.get("ledger_id")
    schema_version = raw.get("schema_version")
    if not isinstance(ledger_id, str) or not ledger_id.strip():
        raise ValueError("backup manifest tombstone ledger identity is invalid")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != TOMBSTONE_SCHEMA_VERSION
    ):
        raise ValueError("backup manifest tombstone ledger schema version is invalid")
    return ledger_id.strip(), schema_version


def _restore_ledger(target: Path, expected_id: str, expected_version: int) -> TombstoneLedger:
    ledger_path = default_tombstone_ledger_path(target)
    if not ledger_path.is_file():
        raise ValueError(f"restore tombstone ledger is missing: {ledger_path}")
    _assert_no_sidecars(ledger_path, "restore tombstone ledger")
    try:
        ledger = TombstoneLedger(ledger_path, create=False)
    except (OSError, TombstoneLedgerError) as error:
        raise ValueError(f"restore tombstone ledger is invalid: {error}") from error
    if ledger.ledger_id != expected_id:
        raise ValueError("restore tombstone ledger identity mismatch")
    if expected_version != TOMBSTONE_SCHEMA_VERSION:
        raise ValueError("restore tombstone ledger schema version mismatch")
    _assert_no_sidecars(ledger_path, "restore tombstone ledger")
    return ledger


def validate_backup(backup_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Validate a backup manifest, byte size, SHA-256 digest, and SQLite integrity."""
    backup, manifest = _resolved(backup_path), _resolved(manifest_path)
    if backup == manifest:
        raise ValueError("backup and manifest paths must be different")
    if not backup.is_file():
        raise FileNotFoundError(f"backup database does not exist: {backup}")
    if not manifest.is_file():
        raise FileNotFoundError(f"backup manifest does not exist: {manifest}")
    _assert_no_sidecars(backup, "backup database")

    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("backup manifest is not valid UTF-8 JSON") from error
    if not isinstance(metadata, dict):
        raise ValueError("backup manifest must be a JSON object")
    ledger_id, ledger_schema_version = _manifest_ledger(metadata)

    expected_size = metadata.get("size")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
        raise ValueError("backup manifest size is invalid")
    actual_size = backup.stat().st_size
    if actual_size != expected_size:
        raise ValueError(f"backup size/checksum mismatch: expected {expected_size} bytes, got {actual_size}")

    expected_sha256 = metadata.get("sha256")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("backup manifest checksum is invalid")
    actual_sha256 = _sha256(backup)
    if actual_sha256 != expected_sha256:
        raise ValueError("backup checksum does not match manifest")

    try:
        with closing(_readonly_connection(backup, immutable=True)) as connection:
            integrity = _integrity_check(connection)
            database_ledger_id, database_ledger_version = _read_ledger_binding(
                connection,
                role="backup database",
            )
    except sqlite3.DatabaseError as error:
        raise ValueError(f"SQLite integrity check failed: {error}") from error
    if (database_ledger_id, database_ledger_version) != (ledger_id, ledger_schema_version):
        raise ValueError("backup database and manifest tombstone ledger identity mismatch")
    _assert_no_sidecars(backup, "backup database")
    return {
        "backup": str(backup),
        "manifest": str(manifest),
        "size": actual_size,
        "sha256": actual_sha256,
        "integrity": integrity,
        "ledger_id": ledger_id,
        "ledger_schema_version": ledger_schema_version,
    }


def read_database_ledger_binding(database_path: str | Path) -> tuple[str, int]:
    """Read a live database's deletion-ledger identity without creating or mutating it."""
    database = _resolved(database_path)
    if not database.is_file():
        raise FileNotFoundError(f"live database does not exist: {database}")
    try:
        with closing(_readonly_connection(database)) as connection:
            return _read_ledger_binding(connection, role="live database")
    except sqlite3.DatabaseError as error:
        raise ValueError(f"live database ledger validation failed: {error}") from error


def validate_upgrade_recovery_set(
    database_path: str | Path,
    backup_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Prove that a verified recovery snapshot belongs to the live database."""
    verified = validate_backup(backup_path, manifest_path)
    live_ledger_id, live_schema_version = read_database_ledger_binding(database_path)
    if (verified["ledger_id"], verified["ledger_schema_version"]) != (
        live_ledger_id,
        live_schema_version,
    ):
        raise ValueError("recovery backup does not belong to the live database")
    return {
        **verified,
        "database": str(_resolved(database_path)),
        "recovery_set": "verified",
    }


def backup_database(source_path: str | Path, backup_path: str | Path) -> Path:
    """Create a consistent SQLite online backup and SHA-256 manifest."""
    source, destination = _resolved(source_path), _resolved(backup_path)
    if source == destination:
        raise ValueError("source and backup paths must be different")
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = destination.with_suffix(destination.suffix + ".manifest.json")
    if manifest in {source, destination}:
        raise ValueError("source, backup, and manifest paths must be different")
    source_ledger = default_tombstone_ledger_path(source)
    if source_ledger in {destination, manifest}:
        raise ValueError("backup destination must not overwrite the source tombstone ledger")

    ledger_id, ledger_schema_version = _ensure_backup_ledger(source)
    _assert_no_sidecars(destination, "backup destination")

    temporary = _temporary_path(destination.parent, destination.name)
    try:
        with (
            closing(_readonly_connection(source)) as source_connection,
            closing(_connection(temporary)) as target_connection,
        ):
            source_connection.backup(target_connection)
            _integrity_check(target_connection)
        _assert_no_sidecars(destination, "backup destination")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    metadata = {
        "format_version": BACKUP_FORMAT_VERSION,
        "sha256": _sha256(destination),
        "size": destination.stat().st_size,
        "integrity": "ok",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tombstone_ledger": {
            "ledger_id": ledger_id,
            "schema_version": ledger_schema_version,
        },
    }
    _write_json_atomic(manifest, metadata)
    return manifest


def _restore_database_atomically(
    backup_path: str | Path,
    manifest_path: str | Path,
    target_path: str | Path,
    *,
    replay: RestoreReplay,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Restore through a temporary DB, requiring replay before atomic replace."""
    backup, manifest, target = (
        _resolved(backup_path),
        _resolved(manifest_path),
        _resolved(target_path),
    )
    if len({backup, manifest, target}) != 3:
        raise ValueError("backup, manifest, and target paths must be different")

    verified = validate_backup(backup, manifest)
    if target.exists() and not confirm_overwrite:
        raise FileExistsError("target database exists; pass --confirm-overwrite to replace it")
    _assert_no_sidecars(target, "restore target")
    ledger = _restore_ledger(
        target,
        str(verified["ledger_id"]),
        int(verified["ledger_schema_version"]),
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target.parent, target.name)
    try:
        try:
            with (
                closing(_readonly_connection(backup, immutable=True)) as source_connection,
                closing(_connection(temporary)) as target_connection,
            ):
                target_connection.row_factory = sqlite3.Row
                source_connection.backup(target_connection)
                tombstones_replayed, claims_removed, events_removed = replay(target_connection, ledger)
                integrity = _integrity_check(target_connection)
        except sqlite3.DatabaseError as error:
            raise ValueError(f"SQLite restore integrity check failed: {error}") from error
        _assert_no_sidecars(target, "restore target")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        **verified,
        "target": str(target),
        "integrity": integrity,
        "tombstones_replayed": tombstones_replayed,
        "claims_removed": claims_removed,
        "events_removed": events_removed,
    }
