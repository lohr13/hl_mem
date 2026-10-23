from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import hl_mem.observability.ops_report as ops_report
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


@pytest.mark.parametrize(
    "status",
    ("success", "ok", "settled", "imported", "estimated", "usage_unknown"),
)
def test_each_producer_success_status_is_successful_in_report_and_health(
    tmp_path: Path,
    status: str,
) -> None:
    path = tmp_path / f"{status}.budget.db"
    governor = _governor(path, Clock(NOW))
    _settle(
        governor,
        UsageAmount(requests=1, input_tokens=2, cost_microunits=3),
        status=status,
        latency_ms=4,
    )

    report = UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())
    health = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert report["totals"]["successes"] == 1
    assert report["totals"]["errors"] == 0
    assert report["groups"][0]["status"] == status
    assert health["failures"] == 0


def test_writer_compatible_non_success_status_is_reported_as_failure(tmp_path: Path) -> None:
    path = tmp_path / "degraded.budget.db"
    governor = _governor(path, Clock(NOW))
    _settle(
        governor,
        UsageAmount(requests=1),
        status="degraded",
        latency_ms=1,
        error_class="Timeout",
    )

    report = UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())
    health = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert report["groups"][0]["status"] == "degraded"
    assert report["totals"]["successes"] == 0
    assert report["totals"]["errors"] == 1
    assert health["failures"] == 1


@pytest.mark.parametrize(
    ("column", "bad_value"),
    (
        ("id", -1),
        ("requests", "private-number-content"),
        ("input_tokens", -1),
        ("cost_microunits", -1),
        ("latency_ms", "NaN"),
        ("attempts", -1),
        ("unknown_outcome", 2),
        ("unknown_cost", 2),
        ("usage_date", "private-date-content"),
        ("created_at", "2026-08-30T12:30:private-timestamp-content+00:00"),
        ("status", "private status content"),
        ("capability", "private capability content"),
        ("operation", "private operation content"),
        ("plugin_id", "private plugin content"),
        ("provider", "private provider content"),
        ("model", "private model content"),
    ),
)
def test_report_wraps_corrupt_event_values_without_disclosure(
    tmp_path: Path,
    column: str,
    bad_value: object,
) -> None:
    path = tmp_path / "corrupt-event.budget.db"
    governor = _governor(path, Clock(NOW))
    _settle(governor, UsageAmount(requests=1), status="success", latency_ms=1)
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE usage_events SET {column}=?", (bad_value,))

    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$") as raised:
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())

    assert str(bad_value) not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("column", "bad_value"),
    (
        ("id", -1),
        ("requests", "private-number-content"),
        ("input_tokens", -1),
        ("cost_microunits", -1),
        ("latency_ms", "NaN"),
        ("attempts", -1),
        ("unknown_outcome", 2),
        ("unknown_cost", 2),
        ("created_at", "private-timestamp-content"),
        ("status", "private status content"),
        ("capability", "private capability content"),
        ("operation", "private operation content"),
        ("plugin_id", "private plugin content"),
        ("provider", "private provider content"),
        ("model", "private model content"),
    ),
)
def test_health_wraps_corrupt_daily_values_without_disclosure(
    tmp_path: Path,
    column: str,
    bad_value: object,
) -> None:
    path = tmp_path / "corrupt-health.budget.db"
    governor = _governor(path, Clock(NOW))
    _settle(governor, UsageAmount(requests=1), status="success", latency_ms=1)
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE usage_events SET {column}=?", (bad_value,))

    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$") as raised:
        UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert str(bad_value) not in str(raised.value)


def test_health_ignores_an_event_moved_outside_its_day_while_report_deep_checks_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "other-day-event.budget.db"
    governor = _governor(path, Clock(NOW))
    _settle(governor, UsageAmount(requests=1), status="success", latency_ms=1)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE usage_events SET usage_date='private-date-content'")

    health = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert health["failures"] == 0
    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$"):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())


@pytest.mark.parametrize(
    ("column", "bad_value"),
    (
        ("created_at", "private-created-at-content"),
        ("lease_expires_at", "private-lease-content"),
        ("finalized_at", "private-finalized-content"),
        ("capability", "private capability content"),
        ("operation", "private operation content"),
        ("plugin_id", "private plugin content"),
        ("provider", "private provider content"),
        ("model", "private model content"),
        ("reserved_requests", "private-number-content"),
        ("reserved_input_tokens", -1),
        ("reserved_cost_microunits", "private-cost-content"),
        ("attempts", -1),
    ),
)
def test_report_and_health_reject_corrupt_reservations_before_filtering(
    tmp_path: Path,
    column: str,
    bad_value: object,
) -> None:
    path = tmp_path / "corrupt-reservation.budget.db"
    governor = _governor(path, Clock(NOW), lease_seconds=300)
    governor.reserve(IDENTITY, UsageAmount(requests=1, input_tokens=2, cost_microunits=3))
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE usage_reservations SET {column}=?", (bad_value,))

    reader = UsageLedgerReader(path)
    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$") as report_error:
        reader.report(WINDOW, limits=UsageLimits())
    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$") as health_error:
        reader.health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert str(bad_value) not in str(report_error.value)
    assert str(bad_value) not in str(health_error.value)


@pytest.mark.parametrize(
    ("column", "bad_value"),
    (("state", "private-state-content"), ("usage_date", "private-date-content")),
)
def test_report_deep_checks_reservations_excluded_from_daily_health(
    tmp_path: Path,
    column: str,
    bad_value: str,
) -> None:
    path = tmp_path / "excluded-reservation.budget.db"
    governor = _governor(path, Clock(NOW))
    governor.reserve(IDENTITY, UsageAmount(requests=1))
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE usage_reservations SET {column}=?", (bad_value,))

    health = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert health["stale_reservations"] == 0
    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$"):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())


@pytest.mark.parametrize(
    ("column", "bad_value"),
    (
        ("created_at", "private-created-at-content"),
        ("finalized_at", "private-finalized-content"),
        ("provider", "private provider content"),
        ("reserved_requests", "private-number-content"),
        ("reserved_input_tokens", -1),
        ("reserved_cost_microunits", "private-cost-content"),
        ("attempts", -1),
    ),
)
def test_report_validates_inactive_reservations_too(
    tmp_path: Path,
    column: str,
    bad_value: str,
) -> None:
    path = tmp_path / "corrupt-inactive-reservation.budget.db"
    governor = _governor(path, Clock(NOW))
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=1))
    governor.release(reservation.id, reason="not_sent")
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE usage_reservations SET {column}=?", (bad_value,))

    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$"):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())


def test_report_normalizes_untrusted_error_class_to_finite_category(tmp_path: Path) -> None:
    path = tmp_path / "unsafe-error-category.budget.db"
    governor = _governor(path, Clock(NOW))
    _settle(
        governor,
        UsageAmount(requests=1),
        status="failed",
        latency_ms=1,
        error_class="Timeout",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE usage_events SET error_class='private_claim_text'")

    report = UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())

    assert report["totals"]["error_categories"] == {"other": 1}
    assert "private_claim_text" not in str(report)


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


def test_health_summary_does_not_use_report_group_or_percentile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
def test_reader_rejects_corrupt_or_newer_ledgers_without_writing(tmp_path: Path, create: object, message: str) -> None:
    path = tmp_path / "invalid.budget.db"
    create(path)  # type: ignore[operator]
    before = (os.stat(path).st_size, os.stat(path).st_mtime_ns)

    with pytest.raises(OpsReportError, match=message):
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits())

    assert (os.stat(path).st_size, os.stat(path).st_mtime_ns) == before


def test_health_summary_has_only_daily_health_fields(tmp_path: Path) -> None:
    path = tmp_path / "usage.budget.db"
    _seed_ledger(path)

    summary = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(10, 100, 1_000), now=NOW)

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


def test_health_uses_indexes_and_skips_one_hundred_thousand_released_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "large-history.budget.db"
    governor = _governor(path, Clock(NOW), lease_seconds=300)
    governor.reserve(IDENTITY, UsageAmount(requests=1))
    _settle(governor, UsageAmount(requests=1), status="success", latency_ms=1)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "WITH RECURSIVE seq(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM seq WHERE n<100000) "
            "INSERT INTO usage_reservations("
            "id,usage_date,capability,operation,plugin_id,provider,model,reserved_requests,"
            "reserved_input_tokens,reserved_output_tokens,reserved_embedding_items,"
            "reserved_rerank_documents,reserved_images,reserved_cost_microunits,attempts,"
            "lease_expires_at,state,final_signature,created_at,finalized_at) "
            "SELECT printf('history-%06d',n),'2026-08-29','llm','extract','hl-mem.builtin',"
            "'dashscope','qwen',1,0,0,0,0,0,0,0,'2026-08-29T13:00:00+00:00','released',"
            "'released','2026-08-29T12:00:00+00:00','2026-08-29T12:01:00+00:00' FROM seq"
        )
        event_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM usage_events WHERE usage_date=?",
                (NOW.date().isoformat(),),
            )
        )
        reservation_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM usage_reservations WHERE usage_date=? AND state='active'",
                (NOW.date().isoformat(),),
            )
        )

    validated_reservations = 0
    validate_reservation = ops_report._validate_reservation_row

    def count_validation(row: sqlite3.Row):
        nonlocal validated_reservations
        validated_reservations += 1
        return validate_reservation(row)

    monkeypatch.setattr(ops_report, "_validate_reservation_row", count_validation)

    summary = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert "USING INDEX idx_usage_events_date_capability" in event_plan
    assert "USING INDEX idx_usage_reservations_active" in reservation_plan
    assert validated_reservations == 1
    assert summary["stale_reservations"] == 0


def test_health_summary_normalizes_offset_reservation_leases_before_aggregation(tmp_path: Path) -> None:
    path = tmp_path / "usage.budget.db"
    governor = _governor(path, Clock(NOW), lease_seconds=300)
    governor.reserve(IDENTITY, UsageAmount(requests=1))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE usage_reservations SET lease_expires_at='2026-08-30T14:00:00+02:00'")

    summary = UsageLedgerReader(path).health_summary(day=NOW.date(), limits=UsageLimits(), now=NOW)

    assert summary["stale_reservations"] == 1


def test_health_summary_keeps_microsecond_lease_ordering_across_offsets(tmp_path: Path) -> None:
    path = tmp_path / "usage.budget.db"
    now = datetime(2026, 8, 30, 13, 0, 0, 500, tzinfo=timezone.utc)
    governor = _governor(path, Clock(now), lease_seconds=300)
    late = governor.reserve(IDENTITY, UsageAmount(requests=2))
    equal = governor.reserve(IDENTITY, UsageAmount(requests=3))
    early = governor.reserve(IDENTITY, UsageAmount(requests=5))
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "UPDATE usage_reservations SET lease_expires_at=? WHERE id=?",
            (
                ("2026-08-30T15:00:00.000700+02:00", late.id),
                ("2026-08-30T13:00:00.000500+00:00", equal.id),
                ("2026-08-30T15:00:00.000499+02:00", early.id),
            ),
        )

    summary = UsageLedgerReader(path).health_summary(
        day=now.date(),
        limits=UsageLimits(daily_requests=10),
        now=now,
    )

    assert summary["stale_reservations"] == 2
    assert summary["utilization"]["requests"] == Decimal("0.2")


@pytest.mark.parametrize("lease_expires_at", ["not-an-iso-timestamp", "2026-08-30T13:00:01.000000"])
def test_invalid_or_naive_reservation_lease_fails_closed(
    tmp_path: Path,
    lease_expires_at: str,
) -> None:
    path = tmp_path / "usage.budget.db"
    governor = _governor(path, Clock(NOW), lease_seconds=300)
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=7))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE usage_reservations SET lease_expires_at=? WHERE id=?",
            (lease_expires_at, reservation.id),
        )

    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$") as raised:
        UsageLedgerReader(path).report(WINDOW, limits=UsageLimits(daily_requests=10))

    assert lease_expires_at not in str(raised.value)


@pytest.mark.parametrize("lease_expires_at", ["not-an-iso-timestamp", "2026-08-30T13:00:01.000000"])
def test_health_summary_wraps_invalid_or_naive_reservation_lease(
    tmp_path: Path,
    lease_expires_at: str,
) -> None:
    path = tmp_path / "usage.budget.db"
    governor = _governor(path, Clock(NOW), lease_seconds=300)
    reservation = governor.reserve(IDENTITY, UsageAmount(requests=7))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE usage_reservations SET lease_expires_at=? WHERE id=?",
            (lease_expires_at, reservation.id),
        )

    with pytest.raises(OpsReportError, match="^usage ledger is unreadable$") as raised:
        UsageLedgerReader(path).health_summary(
            day=NOW.date(),
            limits=UsageLimits(daily_requests=10),
            now=NOW,
        )

    assert lease_expires_at not in str(raised.value)
