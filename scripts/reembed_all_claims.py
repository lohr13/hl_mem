"""Re-embed active claims with the configured document embedding model."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_mem.components import make_embedder  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402


class BatchEmbedder(Protocol):
    model: str
    dim: int

    def embed_batch(self, texts: list[str]) -> list[bytes]: ...


@dataclass(frozen=True)
class TargetClaim:
    claim_id: str
    index_text: str
    embedding_dense: bytes
    embedding_model: str | None
    embedding_dim: int | None


@dataclass(frozen=True)
class Snapshot:
    active_count: int
    active_fingerprint: str
    target_count: int
    target_fingerprint: str
    embedding_fingerprint: str
    guard_fingerprint: str
    targets: tuple[TargetClaim, ...]

    def report(self) -> dict[str, Any]:
        return {
            "active_count": self.active_count,
            "active_fingerprint": self.active_fingerprint,
            "target_count": self.target_count,
            "target_fingerprint": self.target_fingerprint,
            "embedding_fingerprint": self.embedding_fingerprint,
        }


def _update_part(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _fingerprint_ids(ids: Sequence[str]) -> str:
    return hashlib.sha256("".join(ids).encode("utf-8")).hexdigest()


def _snapshot(connection: sqlite3.Connection) -> Snapshot:
    active_ids = [
        str(row[0]) for row in connection.execute("SELECT id FROM claims WHERE status='active' ORDER BY id").fetchall()
    ]
    rows = connection.execute(
        "SELECT id,index_text,embedding_dense,embedding_model,embedding_dim "
        "FROM claims WHERE status='active' AND embedding_dense IS NOT NULL ORDER BY id"
    ).fetchall()
    targets = tuple(
        TargetClaim(
            claim_id=str(row[0]),
            index_text=str(row[1] or ""),
            embedding_dense=bytes(row[2]),
            embedding_model=str(row[3]) if row[3] is not None else None,
            embedding_dim=int(row[4]) if row[4] is not None else None,
        )
        for row in rows
    )
    embedding_hasher = hashlib.sha256()
    guard_hasher = hashlib.sha256()
    for target in targets:
        identifier = target.claim_id.encode("utf-8")
        model = (target.embedding_model or "").encode("utf-8")
        dimension = str(target.embedding_dim or "").encode("ascii")
        dense_hash = hashlib.sha256(target.embedding_dense).digest()
        for part in (identifier, model, dimension, dense_hash):
            _update_part(embedding_hasher, part)
        for part in (identifier, target.index_text.encode("utf-8"), model, dimension, dense_hash):
            _update_part(guard_hasher, part)
    target_ids = [target.claim_id for target in targets]
    return Snapshot(
        active_count=len(active_ids),
        active_fingerprint=_fingerprint_ids(active_ids),
        target_count=len(targets),
        target_fingerprint=_fingerprint_ids(target_ids),
        embedding_fingerprint=embedding_hasher.hexdigest(),
        guard_fingerprint=guard_hasher.hexdigest(),
        targets=targets,
    )


def _embed_targets(embedder: BatchEmbedder, targets: tuple[TargetClaim, ...], *, progress: bool) -> list[bytes]:
    vectors: list[bytes] = []
    batch_size = max(1, int(getattr(embedder, "MAX_BATCH_SIZE", 10)))
    batch_count = (len(targets) + batch_size - 1) // batch_size
    for batch_number, start in enumerate(range(0, len(targets), batch_size), 1):
        batch = targets[start : start + batch_size]
        vectors.extend(embedder.embed_batch([target.index_text for target in batch]))
        if progress and (batch_number == 1 or batch_number % 10 == 0 or batch_number == batch_count):
            print(f"embedded batch {batch_number}/{batch_count}", file=sys.stderr, flush=True)
    return vectors


def reembed_all_claims(
    connection: sqlite3.Connection,
    embedder: BatchEmbedder,
    *,
    dry_run: bool = False,
    progress: bool = False,
) -> dict[str, Any]:
    before = _snapshot(connection)
    if dry_run or not before.targets:
        return {
            "dry_run": dry_run,
            "model": embedder.model,
            "dim": embedder.dim,
            "before": before.report(),
            "after": before.report(),
            "updated_count": 0,
        }

    vectors = _embed_targets(embedder, before.targets, progress=progress)
    if len(vectors) != before.target_count:
        raise ValueError(f"embedding count mismatch: expected {before.target_count}, got {len(vectors)}")
    expected_size = embedder.dim * 4
    if any(len(vector) != expected_size for vector in vectors):
        raise ValueError(f"embedding dimension mismatch: expected {expected_size}-byte BLOBs")

    try:
        connection.execute("BEGIN IMMEDIATE")
        locked = _snapshot(connection)
        if locked.guard_fingerprint != before.guard_fingerprint:
            raise RuntimeError("target claims changed during re-embedding; no embedding updates were written")
        connection.executemany(
            "UPDATE claims SET embedding_dense=?,embedding_model=?,embedding_dim=? WHERE id=?",
            [
                (vector, embedder.model, embedder.dim, target.claim_id)
                for target, vector in zip(before.targets, vectors, strict=True)
            ],
        )
        after = _snapshot(connection)
        if after.target_fingerprint != before.target_fingerprint or after.target_count != before.target_count:
            raise RuntimeError("target claim set changed during re-embedding; transaction rolled back")
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "dry_run": False,
        "model": embedder.model,
        "dim": embedder.dim,
        "before": before.report(),
        "after": after.report(),
        "updated_count": before.target_count,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Inspect the target snapshot without API calls or writes"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "hl_mem.toml")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--db", type=Path)
    parser.add_argument(
        "--backup",
        type=Path,
        default=ROOT / "var" / "hl_mem.db.backup_before_qwen_migration",
        help="Required existing backup for a write run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(args.config, args.env)
    if settings.embedding_api_mode != "native":
        raise SystemExit("embedding.api_mode must be 'native' for this migration")
    if settings.embedding_model != "qwen3.7-text-embedding":
        raise SystemExit("embedding.model must be 'qwen3.7-text-embedding' for this migration")
    if not args.dry_run and not args.backup.is_file():
        raise SystemExit(f"required database backup does not exist: {args.backup}")

    database_path = (args.db or Path(settings.database_path)).resolve()
    connection = sqlite3.connect(database_path, timeout=settings.database_busy_timeout_seconds)
    try:
        connection.execute(f"PRAGMA busy_timeout={settings.database_busy_timeout_seconds * 1000}")
        report = reembed_all_claims(
            connection, make_embedder(settings), dry_run=args.dry_run, progress=not args.dry_run
        )
    finally:
        connection.close()
    report["database_path"] = str(database_path)
    report["backup_path"] = str(args.backup.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
