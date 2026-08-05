from __future__ import annotations

import sqlite3
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scripts.reembed_all_claims import reembed_all_claims  # noqa: E402


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE claims ("
        "id TEXT PRIMARY KEY,status TEXT,index_text TEXT,embedding_dense BLOB,"
        "embedding_model TEXT,embedding_dim INTEGER)"
    )
    connection.executemany(
        "INSERT INTO claims VALUES (?,?,?,?,?,?)",
        (
            ("active-vector", "active", "alpha", struct.pack("<2f", 9.0, 9.0), "old", 2),
            ("active-null", "active", "beta", None, None, None),
            ("expired-vector", "expired", "gamma", struct.pack("<2f", 8.0, 8.0), "old", 2),
        ),
    )
    connection.commit()
    return connection


class _FakeEmbedder:
    model = "qwen3.7-text-embedding"
    dim = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        self.calls.append(list(texts))
        return [struct.pack("<2f", float(index + 1), 0.0) for index, _ in enumerate(texts)]


class ReembedAllClaimsTests(unittest.TestCase):
    def test_dry_run_makes_no_api_call_and_no_database_change(self) -> None:
        connection = _connection()
        embedder = _FakeEmbedder()

        report = reembed_all_claims(connection, embedder, dry_run=True)

        self.assertEqual(report["before"]["target_count"], 1)
        self.assertEqual(report["updated_count"], 0)
        self.assertEqual(embedder.calls, [])
        row = connection.execute(
            "SELECT embedding_model,embedding_dense FROM claims WHERE id='active-vector'"
        ).fetchone()
        self.assertEqual(row, ("old", struct.pack("<2f", 9.0, 9.0)))

    def test_updates_only_active_claims_with_existing_dense_vectors(self) -> None:
        connection = _connection()
        embedder = _FakeEmbedder()

        report = reembed_all_claims(connection, embedder)

        self.assertEqual(embedder.calls, [["alpha"]])
        self.assertEqual(report["updated_count"], 1)
        self.assertNotEqual(report["before"]["embedding_fingerprint"], report["after"]["embedding_fingerprint"])
        rows = connection.execute(
            "SELECT id,embedding_model,embedding_dim,embedding_dense FROM claims ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0][1:], (None, None, None))
        self.assertEqual(rows[1][1:], ("qwen3.7-text-embedding", 2, struct.pack("<2f", 1.0, 0.0)))
        self.assertEqual(rows[2][1:], ("old", 2, struct.pack("<2f", 8.0, 8.0)))

    def test_aborts_without_embedding_writes_when_target_snapshot_drifts(self) -> None:
        connection = _connection()

        class DriftingEmbedder(_FakeEmbedder):
            def embed_batch(self, texts: list[str]) -> list[bytes]:
                connection.execute("UPDATE claims SET index_text='changed' WHERE id='active-vector'")
                connection.commit()
                return super().embed_batch(texts)

        with self.assertRaisesRegex(RuntimeError, "changed during re-embedding"):
            reembed_all_claims(connection, DriftingEmbedder())

        model = connection.execute("SELECT embedding_model FROM claims WHERE id='active-vector'").fetchone()[0]
        self.assertEqual(model, "old")


if __name__ == "__main__":
    unittest.main(verbosity=2)
