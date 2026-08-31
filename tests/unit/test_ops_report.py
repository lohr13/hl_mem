from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hl_mem.errors import OpsReportError
from hl_mem.observability.ops_report import ReportWindow, build_ops_report
from hl_mem.observability.usage import UsageGovernor
from hl_mem.observability.usage_types import UsageAmount, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import ProviderCapability
from hl_mem.settings import Settings

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
WINDOW = ReportWindow(NOW - timedelta(hours=1), NOW)


def _seeded_connection(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    database_path = tmp_path / "memory.db"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, job_type TEXT NOT NULL, payload_json TEXT,
            idempotency_key TEXT, status TEXT NOT NULL, leased_until TEXT,
            last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            heartbeat_at TEXT
        );
        CREATE TABLE conflict_cases (
            id TEXT PRIMARY KEY, pair_key TEXT, left_claim_id TEXT,
            right_claim_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE TABLE deferred_tasks (
            id TEXT PRIMARY KEY, task_type TEXT NOT NULL, resource_type TEXT,
            resource_id TEXT, payload_json TEXT, idempotency_key TEXT,
            status TEXT NOT NULL, attempts INTEGER, max_attempts INTEGER,
            run_after TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)
    connection.commit()
    return connection, database_path


def _inputs(tmp_path: Path, database_path: Path) -> dict[str, object]:
    return {
        "database_path": database_path,
        "usage_path": tmp_path / "missing.budget.db",
        "settings": replace(Settings.for_test(), worker_poll_interval=2.0, worker_job_lease_minutes=5),
        "window": WINDOW,
        "now": NOW,
    }


def _insert_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    job_type: str,
    status: str,
    created_at: datetime,
    leased_until: datetime | None = None,
    heartbeat_at: datetime | None = None,
    last_error: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO jobs(id,job_type,payload_json,idempotency_key,status,leased_until,last_error,created_at,updated_at,heartbeat_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            job_type,
            "{}",
            job_id,
            status,
            leased_until.isoformat() if leased_until else None,
            last_error,
            created_at.isoformat(),
            created_at.isoformat(),
            heartbeat_at.isoformat() if heartbeat_at else None,
        ),
    )
    connection.commit()


def test_empty_report_has_stable_structure_and_unknown_worker(tmp_path: Path) -> None:
    connection, database_path = _seeded_connection(tmp_path)
    try:
        first = build_ops_report(connection, **_inputs(tmp_path, database_path))
        second = build_ops_report(connection, **_inputs(tmp_path, database_path))
    finally:
        connection.close()

    assert first["schema_version"] == 1
    assert first["worker"] == {"state": "unknown", "source": None, "heartbeat_at": None}
    assert first["files"]["shm"]["size_bytes"] == 0
    assert "unknown_usage" in first["warnings"]
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)


def test_report_warns_without_claim_or_error_text_and_reports_backlogs(tmp_path: Path) -> None:
    connection, database_path = _seeded_connection(tmp_path)
    try:
        _insert_job(
            connection,
            job_id="failed",
            job_type="extract_event",
            status="failed",
            created_at=NOW - timedelta(minutes=10),
            last_error="provider raw error: private claim text",
        )
        _insert_job(
            connection,
            job_id="dead",
            job_type="extract_event",
            status="dead",
            created_at=NOW - timedelta(minutes=9),
        )
        _insert_job(
            connection,
            job_id="stale",
            job_type="maintenance",
            status="running",
            created_at=NOW - timedelta(minutes=8),
            leased_until=NOW - timedelta(seconds=1),
            heartbeat_at=NOW - timedelta(seconds=5),
        )
        connection.execute(
            "INSERT INTO conflict_cases(id,pair_key,left_claim_id,right_claim_id,status,created_at) VALUES "
            "('conflict','pair','left','right','manual_required',?)",
            ((NOW - timedelta(minutes=7)).isoformat(),),
        )
        connection.execute(
            "INSERT INTO deferred_tasks(id,task_type,resource_type,resource_id,payload_json,idempotency_key,status,attempts,max_attempts,run_after,created_at,updated_at) "
            "VALUES ('deferred','record_recall_access','claim','claim','{}','deferred','pending',0,3,?,?,?)",
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        connection.commit()

        report = build_ops_report(connection, **_inputs(tmp_path, database_path))
    finally:
        connection.close()

    encoded = json.dumps(report, ensure_ascii=False, default=str)
    assert report["jobs"]["counts_by_status"] == {
        "pending": 0,
        "running": 1,
        "succeeded": 0,
        "failed": 1,
        "dead": 1,
    }
    assert report["jobs"]["expired_running_leases"] == 1
    assert report["jobs"]["recall_side_effect_backlog"] == 1
    assert report["conflicts"]["manual_required_count"] == 1
    assert report["worker"] == {
        "state": "inactive",
        "source": "job_heartbeat",
        "heartbeat_at": (NOW - timedelta(seconds=5)).isoformat(),
    }
    assert {"failed_jobs", "stale_running_jobs", "worker_inactive"}.issubset(report["warnings"])
    assert "private claim text" not in encoded
    assert "provider raw error" not in encoded


def test_report_prefers_process_heartbeat_and_flags_large_wal(tmp_path: Path) -> None:
    connection, database_path = _seeded_connection(tmp_path)
    try:
        wal_path = Path(f"{database_path}-wal")
        with wal_path.open("wb") as wal:
            wal.truncate(256 * 1024 * 1024 + 1)
        report = build_ops_report(
            connection,
            **_inputs(tmp_path, database_path),
            worker_runtime={"running": True, "heartbeat_at": NOW.isoformat()},
        )
    finally:
        connection.close()

    assert report["worker"] == {"state": "active", "source": "process", "heartbeat_at": NOW.isoformat()}
    assert "large_wal" in report["warnings"]


def test_report_warns_for_expired_reservations_and_near_budget(tmp_path: Path) -> None:
    connection, database_path = _seeded_connection(tmp_path)
    usage_path = tmp_path / "usage.budget.db"
    identity = UsageIdentity(ProviderCapability.LLM, "extract", "builtin", "provider", "model")
    limits = UsageLimits(daily_requests=10)
    UsageGovernor(usage_path, limits, lease_seconds=1, now=lambda: NOW - timedelta(minutes=1)).reserve(
        identity, UsageAmount(requests=1)
    )
    UsageGovernor(usage_path, limits, lease_seconds=60, now=lambda: NOW).reserve(identity, UsageAmount(requests=8))
    settings = replace(Settings.for_test(), usage_daily_request_limit=10)
    try:
        report = build_ops_report(
            connection,
            database_path=database_path,
            usage_path=usage_path,
            settings=settings,
            window=WINDOW,
            now=NOW,
        )
    finally:
        connection.close()

    assert {"budget_near_limit", "expired_reservation", "worker_unknown"}.issubset(report["warnings"])


def test_report_rejects_missing_main_database(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        with pytest.raises(OpsReportError, match="main database does not exist"):
            build_ops_report(connection, **_inputs(tmp_path, tmp_path / "missing.db"))
    finally:
        connection.close()
