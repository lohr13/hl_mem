from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.settings import Settings
from hl_mem.storage.backup import backup_database, restore_database, validate_backup
from hl_mem.storage.database import Database


def _seed_database(path: Path, *, event_id: str, claim_id: str | None = None) -> None:
    database = Database(path)
    connection = database.open()
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) " "VALUES (?,?,?,?,?,?)",
        (
            event_id,
            "message",
            "user",
            json.dumps({"text": event_id}),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    if claim_id is not None:
        connection.execute(
            "INSERT INTO claims(id,subject_entity_id,predicate,value_json,recorded_from,status) "
            "VALUES (?,?,?,?,?,?)",
            (
                claim_id,
                "user",
                "preference",
                json.dumps("SQLite"),
                "2026-01-01T00:00:00Z",
                "active",
            ),
        )
    connection.commit()
    database.close()


def _counts(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        events = connection.execute("SELECT count(*) FROM events").fetchone()[0]
        claims = connection.execute("SELECT count(*) FROM claims").fetchone()[0]
        return events, claims
    finally:
        connection.close()


def test_backup_cli_outputs_machine_readable_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    _seed_database(source, event_id="source-event", claim_id="source-claim")
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings())

    cli_module.main(["backup", str(backup), "--db", str(source)])

    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"backup", "manifest", "size", "sha256", "integrity"}
    assert output["backup"] == str(backup.resolve())
    assert output["manifest"] == str((tmp_path / "backup.db.manifest.json").resolve())
    assert output["size"] == backup.stat().st_size
    assert output["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert output["integrity"] == "ok"
    assert _counts(backup) == (1, 1)


def test_restore_requires_confirmation_and_preserves_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event", claim_id="source-claim")
    _seed_database(target, event_id="target-event")
    manifest = backup_database(source, backup)

    with pytest.raises(FileExistsError, match="confirm-overwrite"):
        restore_database(backup, manifest, target)
    assert _counts(target) == (1, 0)

    result = restore_database(backup, manifest, target, confirm_overwrite=True)

    assert result["target"] == str(target.resolve())
    assert result["integrity"] == "ok"
    assert _counts(target) == (1, 1)


@pytest.mark.parametrize("tamper", ["manifest", "backup"])
def test_restore_rejects_tampering_without_changing_target(tmp_path: Path, tamper: str) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    _seed_database(target, event_id="target-event")
    manifest = backup_database(source, backup)
    if tamper == "manifest":
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["sha256"] = "0" * 64
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
    else:
        backup.write_bytes(b"not a SQLite database")

    with pytest.raises(ValueError, match="checksum"):
        restore_database(backup, manifest, target, confirm_overwrite=True)

    assert _counts(target) == (1, 0)
    assert not list(tmp_path.glob(".target.db.*.tmp"))


def test_backup_and_restore_reject_same_resolved_path(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_database(source, event_id="event")
    with pytest.raises(ValueError, match="different"):
        backup_database(source, tmp_path / "." / "source.db")

    backup = tmp_path / "backup.db"
    manifest = backup_database(source, backup)
    with pytest.raises(ValueError, match="different"):
        restore_database(backup, manifest, tmp_path / "." / "backup.db", confirm_overwrite=True)


def test_validate_and_restore_reject_unhashed_backup_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    manifest = backup_database(source, backup)
    sidecar = Path(f"{backup}-wal")
    sidecar.write_bytes(b"untrusted WAL bytes")

    with pytest.raises(ValueError, match="sidecar"):
        validate_backup(backup, manifest)
    with pytest.raises(ValueError, match="sidecar"):
        restore_database(backup, manifest, target)

    assert not target.exists()
    assert sidecar.read_bytes() == b"untrusted WAL bytes"


def test_restore_rejects_target_sidecars_without_changing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    _seed_database(target, event_id="target-event")
    manifest = backup_database(source, backup)
    original = target.read_bytes()
    sidecar = Path(f"{target}-shm")
    sidecar.write_bytes(b"stale shared memory")

    with pytest.raises(ValueError, match="sidecar"):
        restore_database(backup, manifest, target, confirm_overwrite=True)

    assert target.read_bytes() == original
    assert sidecar.read_bytes() == b"stale shared memory"


def test_restore_cli_outputs_integrity_and_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    manifest = backup_database(source, backup)
    monkeypatch.setattr(cli_module, "load_settings", lambda *_: Settings())

    cli_module.main(
        [
            "restore",
            str(backup),
            "--manifest",
            str(manifest),
            "--db",
            str(target),
            "--confirm-overwrite",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert output["target"] == str(target.resolve())
    assert output["integrity"] == "ok"
    assert _counts(target) == (1, 0)
