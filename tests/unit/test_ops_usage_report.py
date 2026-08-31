from __future__ import annotations

import os
import sqlite3
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hl_mem.errors import OpsReportError
from hl_mem.observability.ops_report import ReportWindow, UsageLedgerReader, parse_report_window
from hl_mem.observability.usage import UsageGovernor
from hl_mem.observability.usage_types import UsageAmount, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import ProviderCapability

NOW = datetime(2026, 8, 30, 13, 0, tzinfo=timezone.utc)
WINDOW = ReportWindow(
    since=datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
    until=NOW,
)
IDENTITY = UsageIdentity(ProviderCapability.LLM, "extract", "hl-mem.builtin", "dashscope", "qwen")


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _governor(path: Path, clock: Clock, *, lease_seconds: int = 300) -> UsageGovernor:
    return UsageGovernor(path, UsageLimits(), lease_seconds=lease_seconds, now=clock)


def _settle(
    governor: UsageGovernor,
    amount: UsageAmount,
    *,
    status: str,
    latency_ms: float,
    error_class: str | None = None,
    unknown: bool = False,
) -> None:
    reservation = governor.reserve(IDENTITY, amount)
    governor.mark_attempt(reservation.id)
    if unknown:
        governor.settle_unknown(
            reservation.id,
            amount,
            status=status,
            latency_ms=latency_ms,
            error_class=error_class,
        )
    else:
        governor.settle(
            reservation.id,
            amount,
            status=status,
            latency_ms=latency_ms,
            error_class=error_class,
        )


def _seed_ledger(path: Path) -> None:
    clock = Clock(WINDOW.since)
    governor = _governor(path, clock, lease_seconds=3600)
    _settle(
        governor,
        UsageAmount(requests=1, input_tokens=1, output_tokens=2, embedding_items=3, cost_microunits=10),
        status="success",
        latency_ms=10,
    )
    clock.value += timedelta(minutes=30)
    _settle(
        governor,
        UsageAmount(requests=1, input_tokens=4, output_tokens=5, rerank_documents=6, cost_microunits=20),
        status="success",
        latency_ms=20,
    )
    clock.value = WINDOW.until
    _settle(
        governor,
        UsageAmount(requests=1, input_tokens=7, output_tokens=8, images=9, cost_microunits=30),
        status="success",
        latency_ms=30,
    )
    clock.value = WINDOW.since + timedelta(minutes=15)
    _settle(
        governor,
        UsageAmount(requests=1, input_tokens=10, cost_microunits=40),
        status="failed",
        latency_ms=40,
        error_class="Timeout",
    )
    clock.value = WINDOW.since + timedelta(minutes=45)
    _settle(
        governor,
        UsageAmount(requests=1, output_tokens=11),
        status="unknown",
        latency_ms=50,
        error_class="ConnectionLost",
        unknown=True,
    )

    clock.value = WINDOW.until
    governor.reserve(IDENTITY, UsageAmount(requests=2, input_tokens=12, cost_microunits=13))
    clock.value = WINDOW.since
    governor.reserve(IDENTITY, UsageAmount(requests=3, output_tokens=14, cost_microunits=15))


def test_parse_report_window_accepts_bounded_utc_duration() -> None:
    assert parse_report_window("24h", now=NOW) == ReportWindow(
        since=datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc),
        until=NOW,
    )
    assert parse_report_window("30d", now=NOW) == ReportWindow(
        since=datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc),
        until=NOW,
    )


def test_parse_report_window_rejects_unbounded_or_fractional_values() -> None:
    for value in ("0h", "1.5h", "31d", "720d", "day", "-1h"):
        with pytest.raises(ValueError, match="--since"):
            parse_report_window(value, now=NOW)


def test_reader_does_not_create_a_missing_ledger(tmp_path: Path) -> None:
    path = tmp_path / "missing.budget.db"

    with pytest.raises(OpsReportError, match="does not exist"):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())

    assert not path.exists()
    assert not path.parent.joinpath("missing.budget.db-wal").exists()


def test_report_groups_finalized_events_with_exact_totals_and_boundary_inclusion(tmp_path: Path) -> None:
    path = tmp_path / "usage.budget.db"
    _seed_ledger(path)
    before = (os.stat(path).st_size, os.stat(path).st_mtime_ns)
    with sqlite3.connect(path) as connection:
        version_before = connection.execute("PRAGMA user_version").fetchone()[0]

    report = UsageLedgerReader(path).report(WINDOW, limits=UsageLimits(10, 100, 1_000))

    assert report["window"] == {"since": "2026-08-30T12:00:00+00:00", "until": "2026-08-30T13:00:00+00:00"}
    assert report["groups"] == [
        {
            "capability": "llm",
            "plugin_id": "hl-mem.builtin",
            "provider": "dashscope",
            "model": "qwen",
            "status": "failed",
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 0,
            "total_tokens": 10,
            "embedding_items": 0,
            "rerank_documents": 0,
            "images": 0,
            "cost_microunits": 40,
            "successes": 0,
            "errors": 1,
            "unknown_outcomes": 0,
            "unknown_costs": 0,
            "last_failure_at": "2026-08-30T12:15:00+00:00",
            "latency_ms": {"p50": 40.0, "p95": 40.0},
            "error_categories": {"Timeout": 1},
        },
        {
            "capability": "llm",
            "plugin_id": "hl-mem.builtin",
            "provider": "dashscope",
            "model": "qwen",
            "status": "success",
            "requests": 3,
            "input_tokens": 12,
            "output_tokens": 15,
            "total_tokens": 27,
            "embedding_items": 3,
            "rerank_documents": 6,
            "images": 9,
            "cost_microunits": 60,
            "successes": 3,
            "errors": 0,
            "unknown_outcomes": 0,
            "unknown_costs": 0,
            "last_failure_at": None,
            "latency_ms": {"p50": 20.0, "p95": 30.0},
            "error_categories": {},
        },
        {
            "capability": "llm",
            "plugin_id": "hl-mem.builtin",
            "provider": "dashscope",
            "model": "qwen",
            "status": "unknown",
            "requests": 1,
            "input_tokens": 0,
            "output_tokens": 11,
            "total_tokens": 11,
            "embedding_items": 0,
            "rerank_documents": 0,
            "images": 0,
            "cost_microunits": None,
            "successes": 0,
            "errors": 1,
            "unknown_outcomes": 1,
            "unknown_costs": 1,
            "last_failure_at": "2026-08-30T12:45:00+00:00",
            "latency_ms": {"p50": 50.0, "p95": 50.0},
            "error_categories": {"ConnectionLost": 1},
        },
    ]
    assert report["totals"] == {
        "requests": 5,
        "input_tokens": 22,
        "output_tokens": 26,
        "total_tokens": 48,
        "embedding_items": 3,
        "rerank_documents": 6,
        "images": 9,
        "cost_microunits": None,
        "successes": 3,
        "errors": 2,
        "unknown_outcomes": 1,
        "unknown_costs": 1,
        "last_failure_at": "2026-08-30T12:45:00+00:00",
        "latency_ms": {"p50": 30.0, "p95": 50.0},
        "error_categories": {"ConnectionLost": 1, "Timeout": 1},
    }
    assert report["reservations"] == {
        "active_count": 1,
        "expired_count": 1,
        "reserved": {
            "requests": 2,
            "input_tokens": 12,
            "output_tokens": 0,
            "total_tokens": 12,
            "embedding_items": 0,
            "rerank_documents": 0,
            "images": 0,
            "cost_microunits": 13,
        },
    }
    assert report["budget"] == {
        "requests": {"limit": 10, "used": 7, "utilization": Decimal("0.7")},
        "tokens": {"limit": 100, "used": 60, "utilization": Decimal("0.6")},
        "cost_microunits": {"limit": 1_000, "used": None, "utilization": None},
    }
    assert (os.stat(path).st_size, os.stat(path).st_mtime_ns) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version_before


def test_empty_valid_ledger_reports_zeroes_and_unlimited_budget(tmp_path: Path) -> None:
    path = tmp_path / "empty.budget.db"
    _governor(path, Clock(NOW))

    report = UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())

    assert report["groups"] == []
    assert report["totals"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "embedding_items": 0,
        "rerank_documents": 0,
        "images": 0,
        "cost_microunits": 0,
        "successes": 0,
        "errors": 0,
        "unknown_outcomes": 0,
        "unknown_costs": 0,
        "last_failure_at": None,
        "latency_ms": {"p50": None, "p95": None},
        "error_categories": {},
    }
    assert report["reservations"]["active_count"] == 0
    assert report["reservations"]["expired_count"] == 0
    assert all(item["utilization"] is None for item in report["budget"].values())


def test_historical_report_excludes_reservations_created_after_window_until(tmp_path: Path) -> None:
    path = tmp_path / "usage.budget.db"
    clock = Clock(WINDOW.until + timedelta(hours=1))
    governor = _governor(path, clock, lease_seconds=3600)
    governor.reserve(IDENTITY, UsageAmount(requests=2, input_tokens=12, cost_microunits=13))

    report = UsageLedgerReader(path).report(WINDOW, limits=UsageLimits(10, 100, 1_000))

    assert report["reservations"] == {
        "active_count": 0,
        "expired_count": 0,
        "reserved": {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "embedding_items": 0,
            "rerank_documents": 0,
            "images": 0,
            "cost_microunits": 0,
        },
    }
    assert report["budget"] == {
        "requests": {"limit": 10, "used": 0, "utilization": Decimal("0")},
        "tokens": {"limit": 100, "used": 0, "utilization": Decimal("0")},
        "cost_microunits": {"limit": 1_000, "used": 0, "utilization": Decimal("0")},
    }


def test_health_summary_does_not_use_report_group_or_percentile_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "usage.budget.db"
    _seed_ledger(path)

    def fail_if_grouped(value: dict[str, object]) -> dict[str, object]:
        raise AssertionError("health summary must not build report groups or percentiles")

    monkeypatch.setattr(UsageLedgerReader, "_finish", staticmethod(fail_if_grouped))

    assert UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW) == {
        "failures": 2,
        "stale_reservations": 1,
        "utilization": {"requests": None, "tokens": None, "cost_microunits": None},
        "unknown_outcomes": 1,
        "unknown_costs": 1,
    }


def _set_newer_schema(path: Path) -> None:
    _governor(path, Clock(NOW))
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=2")


@pytest.mark.parametrize(
    ("create", "message"),
    [
        (lambda path: path.write_text("not a database", encoding="utf-8"), "unreadable"),
        (
            lambda path: _set_newer_schema(path),
            "newer than supported",
        ),
    ],
)
def test_reader_rejects_corrupt_or_newer_ledgers_without_writing(
    tmp_path: Path, create: object, message: str
) -> None:
    path = tmp_path / "invalid.budget.db"
    create(path)  # type: ignore[operator]
    before = (os.stat(path).st_size, os.stat(path).st_mtime_ns)

    with pytest.raises(OpsReportError, match=message):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())

    assert (os.stat(path).st_size, os.stat(path).st_mtime_ns) == before


def test_health_summary_has_only_daily_health_fields(tmp_path: Path) -> None:
    path = tmp_path / "usage.budget.db"
    _seed_ledger(path)

    summary = UsageLedgerReader(path).health_summary(
        day=NOW.date(), limits=UsageLimits(10, 100, 1_000), now=NOW
    )

    assert summary == {
        "failures": 2,
        "stale_reservations": 1,
        "utilization": {
            "requests": Decimal("0.7"),
            "tokens": Decimal("0.6"),
            "cost_microunits": None,
        },
        "unknown_outcomes": 1,
        "unknown_costs": 1,
    }
