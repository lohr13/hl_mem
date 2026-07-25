"""P0-2：usefulness 奖励后的有效过期时间回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.ttl import expire_claims


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


def test_bonus_delays_expiration_and_valid_to_uses_effective_time(tmp_path: Path) -> None:
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
