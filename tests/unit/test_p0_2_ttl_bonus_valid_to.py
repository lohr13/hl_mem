"""P0-2：usefulness 奖励后的有效过期时间回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.ttl import expire_claims


class _BeforeBeginConnection:
    """在首次写事务加锁前注入另一个真实连接的并发提交。"""

    def __init__(self, connection: sqlite3.Connection, before_begin: Any) -> None:
        self._connection = connection
        self._before_begin = before_begin
        self._injected = False

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        if sql == "BEGIN IMMEDIATE" and not self._injected:
            self._injected = True
            self._before_begin()
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _insert_claim_with_bonus(
    connection: sqlite3.Connection,
    *,
    claim_id: str,
    expires_at: str,
    bonus_days: int,
    canonical_slot: str | None = None,
    observed_at: str | None = None,
) -> None:
    """写入带 usefulness 奖励的 active Claim。"""
    repository = ClaimRepository(connection)
    repository.insert_claim(
        {
            "id": claim_id,
            "namespace_key": "default",
            "recorded_from": observed_at or "2026-01-01T00:00:00+00:00",
            "observed_at": observed_at,
            "status": "active",
            "scope": "temporal",
            "expires_at": expires_at,
            "canonical_slot": canonical_slot,
        }
    )
    connection.execute(
        "INSERT INTO memory_usefulness(memory_type,memory_id,retention_bonus_days,updated_at) VALUES(?,?,?,?)",
        ("claim", claim_id, bonus_days, "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()


def test_bonus_delays_expiration_and_valid_to_uses_effective_time(
    tmp_path: Path,
) -> None:
    """奖励生效时，关闭时间必须是奖励后的 effective expiration。"""
    database = Database(tmp_path / "ttl-bonus.db")
    connection = database.open()
    try:
        _insert_claim_with_bonus(
            connection,
            claim_id="bonus",
            expires_at="2026-01-10T00:00:00+00:00",
            bonus_days=14,
        )

        assert expire_claims(connection, "2026-01-20T00:00:00+00:00", "on") == {"expired": 0}
        assert expire_claims(connection, "2026-01-25T00:00:00+00:00", "on") == {"expired": 1}
        row = connection.execute("SELECT status,valid_to FROM claims WHERE id=?", ("bonus",)).fetchone()
        assert row["status"] == "expired"
        assert row["valid_to"] == "2026-01-24T00:00:00+00:00"
    finally:
        database.close()


def test_short_ttl_slot_still_caps_feedback_bonus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """短 TTL slot 的硬上限不得被 usefulness 奖励延长。"""
    monkeypatch.setenv("HL_MEM_SLOT_SHORT_TTL_SECONDS", "86400")
    database = Database(tmp_path / "ttl-short-slot.db")
    connection = database.open()
    try:
        _insert_claim_with_bonus(
            connection,
            claim_id="short",
            expires_at="2026-01-10T00:00:00+00:00",
            bonus_days=14,
            canonical_slot="state.service_health",
            observed_at="2026-01-01T00:00:00+00:00",
        )

        assert expire_claims(connection, "2026-01-03T00:00:00+00:00", "on") == {"expired": 1}
        row = connection.execute("SELECT valid_to FROM claims WHERE id=?", ("short",)).fetchone()
        assert row["valid_to"] == "2026-01-02T00:00:00+00:00"
    finally:
        database.close()


def test_concurrent_bonus_committed_before_write_lock_prevents_expiration(
    tmp_path: Path,
) -> None:
    """第二连接在加锁前增加 bonus 时，TTL 必须在事务内重读并保留 Claim。"""
    database = Database(tmp_path / "ttl-concurrency.db")
    expiration_connection = database.open()
    concurrent_connection = database.open()
    try:
        _insert_claim_with_bonus(
            expiration_connection,
            claim_id="bonus-race",
            expires_at="2026-01-10T00:00:00+00:00",
            bonus_days=0,
        )

        def add_bonus() -> None:
            concurrent_connection.execute(
                "UPDATE memory_usefulness SET retention_bonus_days=?,updated_at=? "
                "WHERE memory_type=? AND memory_id=?",
                (30, "2026-01-20T00:00:00+00:00", "claim", "bonus-race"),
            )
            concurrent_connection.commit()

        result = expire_claims(
            _BeforeBeginConnection(expiration_connection, add_bonus),
            "2026-01-20T00:00:00+00:00",
            "on",
        )

        row = expiration_connection.execute(
            "SELECT status,valid_to FROM claims WHERE id=?",
            ("bonus-race",),
        ).fetchone()
        assert result == {"expired": 0}
        assert tuple(row) == ("active", None)
    finally:
        database.close()
