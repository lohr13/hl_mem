"""Deletion-aware restore orchestration above the storage backup primitive."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hl_mem.application.deletion import DeletionService
from hl_mem.storage.backup import _restore_database_atomically
from hl_mem.storage.tombstones import TombstoneLedger


def _replay_tombstones(
    connection: sqlite3.Connection,
    ledger: TombstoneLedger,
) -> tuple[int, int, int]:
    tombstones_replayed = 0
    claims_removed = 0
    events_removed = 0
    deletion = DeletionService(connection, ledger_path=ledger.path)
    for entry in ledger.entries():
        replayed = deletion.replay_tombstone(entry)
        tombstones_replayed += 1
        claims_removed += replayed.claims_removed
        events_removed += replayed.events_removed
    return tombstones_replayed, claims_removed, events_removed


def restore_database(
    backup_path: str | Path,
    manifest_path: str | Path,
    target_path: str | Path,
    *,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Validate ledger identity, replay all tombstones, then expose the restored DB."""
    return _restore_database_atomically(
        backup_path,
        manifest_path,
        target_path,
        replay=_replay_tombstones,
        confirm_overwrite=confirm_overwrite,
    )
