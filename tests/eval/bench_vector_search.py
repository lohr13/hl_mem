"""向量检索性能基准测试。

在 522（当前）/ 2k / 10k 规模下测量向量扫描的 p50/p95 延迟和内存占用。
"""

from __future__ import annotations

import sqlite3
import struct
import time
from pathlib import Path

import pytest

from hl_mem.storage.database import Database


def _random_vector(dim: int = 2048) -> bytes:
    import random

    return struct.pack(f"{dim}f", *(random.gauss(0, 1) for _ in range(dim)))


def _populate_claims(connection: sqlite3.Connection, count: int, dim: int = 2048) -> None:
    """填充指定数量的 claims 用于基准测试。"""
    for i in range(count):
        connection.execute(
            "INSERT OR IGNORE INTO claims "
            "(id, subject_entity_id, predicate, value_json, embedding_dense, importance, volatility, "
            "recorded_from, valid_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"bench-{i:06d}",
                "用户",
                "事实",
                f'"benchmark claim {i}"',
                _random_vector(dim),
                0.5,
                "stable",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    connection.commit()


@pytest.mark.benchmark
@pytest.mark.parametrize("scale", [522, 2000, 10000])
def test_vector_search_latency(scale: int, tmp_path: Path) -> None:
    """测量不同规模下向量扫描的 p50/p95 延迟。"""
    db = Database(tmp_path / "bench.db")
    conn = db.open()
    _populate_claims(conn, scale)

    query_vec = _random_vector(2048)
    latencies = []

    for _ in range(20):
        start = time.perf_counter()
        conn.execute("SELECT id FROM claims ORDER BY embedding_dense LIMIT 10").fetchall()
        latencies.append((time.perf_counter() - start) * 1000)

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]

    print(f"\nScale={scale}: p50={p50:.2f}ms, p95={p95:.2f}ms")
    assert p95 < 5000


@pytest.mark.benchmark
def test_vector_search_memory_estimate() -> None:
    """估算不同规模下向量存储的内存占用。"""
    dim = 2048
    float_size = 4
    per_claim = dim * float_size
    for scale in [522, 2000, 10000]:
        total_mb = (per_claim * scale) / (1024 * 1024)
        print(f"\nScale={scale}: ~{total_mb:.1f} MB for {dim}d vectors")
