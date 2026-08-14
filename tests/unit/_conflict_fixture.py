"""测试专用的 041 前历史冲突组构造工具。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

GUARD_TRIGGER_NAMES = (
    "claims_active_exclusive_guard_insert",
    "claims_active_exclusive_guard_update",
)
MIGRATION_041 = (
    Path(__file__).resolve().parents[2] / "src" / "hl_mem" / "storage" / "migrations" / "041_active_claim_guard.sql"
)


@contextmanager
def seed_pre_041_history(connection: sqlite3.Connection) -> Iterator[None]:
    """暂时移除 041 guard 以构造历史脏数据，并在被测操作前恢复。"""
    for trigger_name in GUARD_TRIGGER_NAMES:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    connection.commit()
    try:
        yield
    finally:
        if connection.in_transaction:
            connection.commit()
        connection.executescript(MIGRATION_041.read_text(encoding="utf-8"))
        connection.commit()
