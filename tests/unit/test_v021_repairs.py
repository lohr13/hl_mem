"""v0.21 冲突终态与短 TTL 存量修复测试。"""

from __future__ import annotations

import importlib
import sqlite3
import sys

import pytest

from hl_mem.domain.claims.retention import TTLPolicy
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


def _insert_claim(
    connection,
    claim_id: str,
    *,
    status: str = "active",
    scope: str = "permanent",
    canonical_attribute: str = "fact.other",
    canonical_slot: str | None = None,
    expires_at: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO claims("
        "id,namespace_key,recorded_from,observed_at,status,scope,importance,volatility,"
        "canonical_attribute,canonical_slot,expires_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            "default",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            status,
            scope,
            0.8,
            "stable",
            canonical_attribute,
            canonical_slot,
            expires_at,
        ),
    )


def test_repair_conflict_losers_defaults_to_dry_run_then_applies(tmp_path) -> None:
    repair_conflict_losers = importlib.import_module("hl_mem.workers.repair_conflict_losers").repair_conflict_losers
    connection = Database(tmp_path / "repair-conflicts.db").open()
    _insert_claim(connection, "winner", status="active")
    _insert_claim(connection, "loser", status="disputed")
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,created_at,resolved_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            "case",
            "pair",
            "winner",
            "loser",
            "resolved",
            "keep_left",
            "2026-01-01T00:00:00+00:00",
            "2026-01-03T00:00:00+00:00",
        ),
    )
    connection.commit()

    assert repair_conflict_losers(connection) == {
        "matched": 1,
        "repaired": 0,
        "cas_skipped": 0,
        "dry_run": True,
    }
    assert connection.execute("SELECT status FROM claims WHERE id='loser'").fetchone()[0] == "disputed"

    assert repair_conflict_losers(connection, dry_run=False) == {
        "matched": 1,
        "repaired": 1,
        "cas_skipped": 0,
        "dry_run": False,
    }
    loser = connection.execute(
        "SELECT status,superseded_by_id,valid_to,recorded_to FROM claims WHERE id='loser'"
    ).fetchone()
    assert tuple(loser) == (
        "superseded",
        "winner",
        "2026-01-03T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    )

    _insert_claim(connection, "historical-winner", status="superseded")
    _insert_claim(connection, "historical-loser", status="disputed")
    connection.execute(
        "INSERT INTO conflict_cases("
        "id,pair_key,left_claim_id,right_claim_id,status,decision,created_at,resolved_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            "historical-case",
            "historical-pair",
            "historical-winner",
            "historical-loser",
            "resolved",
            "keep_left",
            "2026-01-04T00:00:00+00:00",
            "2026-01-05T00:00:00+00:00",
        ),
    )
    connection.commit()

    assert repair_conflict_losers(connection)["matched"] == 1
    assert repair_conflict_losers(connection, dry_run=False)["repaired"] == 1
    assert tuple(
        connection.execute("SELECT status,superseded_by_id FROM claims WHERE id='historical-loser'").fetchone()
    ) == ("superseded", "historical-winner")


def test_backfill_short_ttl_defaults_to_dry_run_and_preserves_non_short_slot(tmp_path) -> None:
    backfill_short_ttl = importlib.import_module("hl_mem.workers.backfill_short_ttl").backfill_short_ttl
    connection = Database(tmp_path / "backfill-short-ttl.db").open()
    _insert_claim(
        connection,
        "missing-slot",
        scope="temporal",
        canonical_attribute="state.service_health",
        expires_at="2026-01-15T00:00:00+00:00",
    )
    _insert_claim(
        connection,
        "permanent-short-slot",
        scope="permanent",
        canonical_attribute="fact.other",
        canonical_slot="state.service_health",
    )
    _insert_claim(
        connection,
        "non-short-slot",
        scope="permanent",
        canonical_attribute="state.service_health",
        canonical_slot="state.process",
    )
    _insert_claim(
        connection,
        "permanent-missing-slot",
        scope="permanent",
        canonical_attribute="state.service_health",
    )
    connection.commit()
    policy = TTLPolicy(slot_short_ttl_seconds=86400)

    assert backfill_short_ttl(connection, policy) == {
        "matched": 3,
        "updated": 3,
        "scope_changed": 2,
        "expires_at_changed": 3,
        "applied": 0,
        "cas_skipped": 0,
        "dry_run": True,
    }
    assert connection.execute("SELECT scope FROM claims WHERE id='permanent-short-slot'").fetchone()[0] == "permanent"

    result = backfill_short_ttl(connection, policy, dry_run=False)
    assert result == {
        "matched": 3,
        "updated": 3,
        "scope_changed": 2,
        "expires_at_changed": 3,
        "applied": 3,
        "cas_skipped": 0,
        "dry_run": False,
    }
    rows = connection.execute("SELECT id,scope,canonical_slot,expires_at FROM claims ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [
        ("missing-slot", "temporal", None, "2026-01-02T00:00:00+00:00"),
        ("non-short-slot", "permanent", "state.process", None),
        ("permanent-missing-slot", "temporal", None, "2026-01-02T00:00:00+00:00"),
        (
            "permanent-short-slot",
            "temporal",
            "state.service_health",
            "2026-01-02T00:00:00+00:00",
        ),
    ]


@pytest.mark.parametrize(
    "module_name",
    [
        "hl_mem.workers.repair_conflict_losers",
        "hl_mem.workers.backfill_short_ttl",
    ],
)
def test_repair_cli_default_dry_run_does_not_create_database(module_name, monkeypatch, tmp_path) -> None:
    module = importlib.import_module(module_name)
    missing_database = tmp_path / f"{module_name.rsplit('.', 1)[-1]}.db"
    monkeypatch.setattr(module, "load_settings", lambda *_args: Settings.for_test())
    monkeypatch.setattr(sys, "argv", [module_name, "--db", str(missing_database)])

    with pytest.raises(sqlite3.OperationalError):
        module.main()

    assert not missing_database.exists()
