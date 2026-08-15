from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import hl_mem.cli as cli_module
from hl_mem.application.deletion import DeletionService
from hl_mem.application.forget import ForgetService
from hl_mem.application.restore import restore_database
from hl_mem.settings import Settings
from hl_mem.storage.backup import (
    BACKUP_FORMAT_VERSION,
    backup_database,
    validate_backup,
)
from hl_mem.storage.database import Database
from hl_mem.storage.tombstones import TombstoneLedger, default_tombstone_ledger_path


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
        connection.execute(
            "INSERT INTO evidence_links("
            "id,derived_type,derived_id,evidence_type,evidence_id,relation"
            ") VALUES (?, 'claim', ?, 'event', ?, 'supports')",
            (f"link-{claim_id}-{event_id}", claim_id, event_id),
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


def _copy_restore_ledger(source: Path, target: Path) -> Path:
    source_ledger = default_tombstone_ledger_path(source)
    target_ledger = default_tombstone_ledger_path(target)
    with (
        closing(sqlite3.connect(source_ledger)) as source_connection,
        closing(sqlite3.connect(target_ledger)) as target_connection,
    ):
        source_connection.backup(target_connection)
    return target_ledger


def _add_claim_with_event(path: Path, *, event_id: str, claim_id: str) -> None:
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
    connection.execute(
        "INSERT INTO claims(id,subject_entity_id,predicate,value_json,recorded_from,status) " "VALUES (?,?,?,?,?,?)",
        (
            claim_id,
            "user",
            "preference",
            json.dumps(claim_id),
            "2026-01-01T00:00:00Z",
            "active",
        ),
    )
    connection.execute(
        "INSERT INTO evidence_links("
        "id,derived_type,derived_id,evidence_type,evidence_id,relation"
        ") VALUES (?, 'claim', ?, 'event', ?, 'supports')",
        (f"link-{claim_id}-{event_id}", claim_id, event_id),
    )
    connection.commit()
    database.close()


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
    assert set(output) == {
        "backup",
        "manifest",
        "size",
        "sha256",
        "integrity",
        "ledger_id",
        "ledger_schema_version",
    }
    assert output["backup"] == str(backup.resolve())
    assert output["manifest"] == str((tmp_path / "backup.db.manifest.json").resolve())
    assert output["size"] == backup.stat().st_size
    assert output["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert output["integrity"] == "ok"
    metadata = json.loads((tmp_path / "backup.db.manifest.json").read_text(encoding="utf-8"))
    assert metadata["format_version"] == 2
    assert metadata["tombstone_ledger"] == {
        "ledger_id": output["ledger_id"],
        "schema_version": output["ledger_schema_version"],
    }
    assert _counts(backup) == (1, 1)


def test_restore_requires_confirmation_and_preserves_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event", claim_id="source-claim")
    _seed_database(target, event_id="target-event")
    manifest = backup_database(source, backup)
    _copy_restore_ledger(source, target)

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
    _copy_restore_ledger(source, target)
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


def test_backup_rejects_destination_that_would_overwrite_tombstone_ledger(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_database(source, event_id="event")
    ledger_path = default_tombstone_ledger_path(source)

    with pytest.raises(ValueError, match="tombstone ledger"):
        backup_database(source, ledger_path)

    assert not ledger_path.exists()
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT 1 FROM deletion_ledger_state").fetchone() is None


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
    _copy_restore_ledger(source, target)
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
    _copy_restore_ledger(source, target)
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
    assert output["ledger_id"]
    assert output["tombstones_replayed"] == 0
    assert _counts(target) == (1, 0)


def test_backup_manifest_v2_binds_main_database_and_sidecar_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    _seed_database(source, event_id="source-event", claim_id="source-claim")

    manifest = backup_database(source, backup)

    assert BACKUP_FORMAT_VERSION == 2
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    ledger_metadata = metadata["tombstone_ledger"]
    ledger = TombstoneLedger(default_tombstone_ledger_path(source), create=False)
    with sqlite3.connect(source) as connection:
        binding = connection.execute(
            "SELECT ledger_id,schema_version FROM deletion_ledger_state WHERE singleton=1"
        ).fetchone()
    assert tuple(binding) == (ledger.ledger_id, ledger_metadata["schema_version"])
    assert ledger_metadata == {
        "ledger_id": ledger.ledger_id,
        "schema_version": 1,
    }
    verified = validate_backup(backup, manifest)
    assert verified["ledger_id"] == ledger.ledger_id
    assert verified["ledger_schema_version"] == 1


def test_restore_rejects_legacy_manifest_without_deletion_history_proof(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    manifest = backup_database(source, backup)
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    metadata["format_version"] = 1
    metadata.pop("tombstone_ledger")
    manifest.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="legacy.*no tombstone ledger identity"):
        validate_backup(backup, manifest)
    with pytest.raises(ValueError, match="legacy.*no tombstone ledger identity"):
        restore_database(backup, manifest, target)

    assert not target.exists()


def test_restore_rejects_missing_target_ledger_before_exposing_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    manifest = backup_database(source, backup)

    with pytest.raises(ValueError, match="restore tombstone ledger is missing"):
        restore_database(backup, manifest, target)

    assert not target.exists()


def test_restore_rejects_mismatched_ledger_without_changing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    target = tmp_path / "target.db"
    _seed_database(source, event_id="source-event")
    _seed_database(target, event_id="target-event")
    manifest = backup_database(source, backup)
    original = target.read_bytes()
    mismatched = TombstoneLedger(default_tombstone_ledger_path(target))

    with pytest.raises(ValueError, match="ledger identity mismatch"):
        restore_database(backup, manifest, target, confirm_overwrite=True)

    assert mismatched.ledger_id
    assert target.read_bytes() == original
    assert _counts(target) == (1, 0)


def test_old_snapshot_restore_replays_tombstone_closure_before_atomic_replace(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "old-snapshot.db"
    _seed_database(source, event_id="deleted-event", claim_id="deleted-claim")
    _add_claim_with_event(source, event_id="survivor-event", claim_id="survivor-claim")
    database = Database(source)
    connection = database.open()
    connection.execute(
        "INSERT INTO memory_relations(id,from_id,to_id,relation,created_at) "
        "VALUES ('relation','deleted-claim','survivor-claim','supports','2026-01-01T00:00:00Z')"
    )
    connection.commit()
    database.close()
    manifest = backup_database(source, backup)

    database = Database(source)
    connection = database.open()
    DeletionService(connection).delete_claim("deleted-claim")
    database.close()

    result = restore_database(backup, manifest, source, confirm_overwrite=True)

    with sqlite3.connect(source) as restored:
        assert restored.execute("SELECT id FROM claims ORDER BY id").fetchall() == [("survivor-claim",)]
        assert restored.execute("SELECT id FROM events ORDER BY id").fetchall() == [("survivor-event",)]
        assert restored.execute("SELECT 1 FROM memory_relations").fetchone() is None
        assert (
            restored.execute(
                "SELECT 1 FROM evidence_links WHERE derived_id='deleted-claim' OR evidence_id='deleted-event'"
            ).fetchone()
            is None
        )
    assert result["tombstones_replayed"] == 1
    assert result["claims_removed"] == 1
    assert result["events_removed"] == 1


def test_delete_then_backup_then_restore_keeps_deleted_content_absent(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "post-delete.db"
    target = tmp_path / "restored.db"
    _seed_database(source, event_id="deleted-event", claim_id="deleted-claim")
    database = Database(source)
    connection = database.open()
    ForgetService(connection).forget("deleted-claim")
    database.close()

    manifest = backup_database(source, backup)
    _copy_restore_ledger(source, target)
    result = restore_database(backup, manifest, target)

    assert _counts(target) == (0, 0)
    assert result["tombstones_replayed"] == 1
    with sqlite3.connect(target) as restored:
        assert restored.execute("SELECT 1 FROM evidence_links").fetchone() is None
        assert restored.execute("SELECT 1 FROM memory_relations").fetchone() is None


def test_tombstone_replay_can_resume_idempotently_after_half_batch(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "old-snapshot.db"
    staging = tmp_path / "staging.db"
    _seed_database(source, event_id="event-a", claim_id="claim-a")
    _add_claim_with_event(source, event_id="event-b", claim_id="claim-b")
    backup_database(source, backup)

    database = Database(source)
    connection = database.open()
    DeletionService(connection).delete_claim("claim-a")
    DeletionService(connection).delete_claim("claim-b")
    database.close()
    ledger_path = default_tombstone_ledger_path(source)
    ledger = TombstoneLedger(ledger_path, create=False)
    entries = ledger.entries()
    assert len(entries) == 2
    with (
        closing(sqlite3.connect(backup)) as backup_connection,
        closing(sqlite3.connect(staging)) as staging_connection,
    ):
        backup_connection.backup(staging_connection)

    database = Database(staging)
    connection = database.open()
    first = DeletionService(connection, ledger_path=ledger_path).replay_tombstone(entries[0])
    database.close()
    assert first.claims_removed == 1
    assert _counts(staging) == (1, 1)

    database = Database(staging)
    connection = database.open()
    replayed = [DeletionService(connection, ledger_path=ledger_path).replay_tombstone(entry) for entry in entries]
    database.close()

    assert replayed[0].claims_removed == 0
    assert replayed[1].claims_removed == 1
    assert _counts(staging) == (0, 0)
