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
from typing import Any

BACKUP_FORMAT_VERSION = 1
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _readonly_connection(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"{path.as_uri()}?{query}", uri=True)


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
    if not isinstance(metadata, dict) or metadata.get("format_version") != BACKUP_FORMAT_VERSION:
        raise ValueError("backup manifest version is invalid")

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
    except sqlite3.DatabaseError as error:
        raise ValueError(f"SQLite integrity check failed: {error}") from error
    _assert_no_sidecars(backup, "backup database")
    return {
        "backup": str(backup),
        "manifest": str(manifest),
        "size": actual_size,
        "sha256": actual_sha256,
        "integrity": integrity,
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
    _assert_no_sidecars(destination, "backup destination")

    temporary = _temporary_path(destination.parent, destination.name)
    try:
        with (
            closing(_readonly_connection(source)) as source_connection,
            closing(sqlite3.connect(temporary)) as target_connection,
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
    }
    _write_json_atomic(manifest, metadata)
    return manifest


def restore_database(
    backup_path: str | Path,
    manifest_path: str | Path,
    target_path: str | Path,
    *,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Validate and restore through a same-directory temporary file and atomic replace."""
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

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(target.parent, target.name)
    try:
        try:
            with (
                closing(_readonly_connection(backup, immutable=True)) as source_connection,
                closing(sqlite3.connect(temporary)) as target_connection,
            ):
                source_connection.backup(target_connection)
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
    }
