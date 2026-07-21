"""从运行库安全复制冻结评测快照并生成脱敏 manifest。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_snapshot(source: str | Path, target: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """使用 SQLite backup API 创建一致副本并输出不含原文的统计 manifest。"""
    source_path, target_path = Path(source).resolve(), Path(target).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"源数据库不存在: {source_path}")
    if source_path == target_path:
        raise ValueError("源数据库和快照目标不能相同")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target_path)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
        tables = {row[0] for row in target_connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        status_counts = (
            dict(target_connection.execute("SELECT status,count(*) FROM claims GROUP BY status").fetchall())
            if "claims" in tables else {}
        )
        versions = (
            [row[0] for row in target_connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            if "schema_migrations" in tables else []
        )
        counts = {
            table: target_connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in ("events", "claims") if table in tables
        }
    finally:
        target_connection.close()
        source_connection.close()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": _hash(source_path),
        "snapshot_sha256": _hash(target_path),
        "schema_versions": versions,
        "counts": counts,
        "claim_status_counts": status_counts,
    }
    manifest_target = Path(manifest_path)
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    manifest_target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _main() -> int:
    parser = argparse.ArgumentParser(description="构建 HL-Mem 离线评测快照")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    arguments = parser.parse_args()
    build_snapshot(arguments.source, arguments.target, arguments.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
