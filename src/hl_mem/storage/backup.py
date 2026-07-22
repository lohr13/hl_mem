"""SQLite 在线备份、清单生成与校验恢复。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_database(source_path: str | Path, backup_path: str | Path) -> Path:
    """使用 SQLite 在线备份 API 创建一致副本及 SHA-256 清单。"""
    source, destination = Path(source_path), Path(backup_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(destination) as target_connection:
        source_connection.backup(target_connection)
    manifest = destination.with_suffix(destination.suffix + ".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest


def restore_database(backup_path: str | Path, manifest_path: str | Path, target_path: str | Path) -> None:
    """校验备份清单后，通过 SQLite 在线备份 API 恢复。"""
    backup, manifest, target = Path(backup_path), Path(manifest_path), Path(target_path)
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("format_version") != 1 or metadata.get("sha256") != _sha256(backup):
        raise ValueError("backup checksum or manifest version is invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
