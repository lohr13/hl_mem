"""Read-only Provider usage reports with deterministic nearest-rank latency."""

from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Mapping, cast
from urllib.parse import quote

from hl_mem.errors import OpsReportError
from hl_mem.observability.usage import USAGE_LEDGER_SCHEMA_VERSION
from hl_mem.observability.usage_types import _LABEL_PATTERN, UsageIdentity, UsageLimits
from hl_mem.plugins.contracts import ProviderCapability
from hl_mem.settings import Settings
from hl_mem.storage.jobs import JobRepository

_DURATION = re.compile(r"([1-9][0-9]*)([hd])$")
_SUCCESS = frozenset("success ok settled imported estimated usage_unknown".split())
_RESERVATION_STATES = frozenset({"active", "released", "settled"})
_SAFE_ERROR_CATEGORIES = frozenset(
    (
        "ConnectionLost LeaseExpired RuntimeError Timeout TypeError ValueError auth circuit_open "
        "deadline_timeout http_error http_timeout invalid_response provider_error quota rate_limit upstream"
    ).split()
)
_AMOUNTS = ("requests", "input_tokens", "output_tokens", "embedding_items", "rerank_documents", "images")
OPS_REPORT_SCHEMA_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class ReportWindow:
    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        if self.since.tzinfo is None or self.until.tzinfo is None:
            raise ValueError("report window bounds must be timezone-aware")
        if self.since > self.until:
            raise ValueError("report window since must not be after until")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("report window now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | str) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be valid offset-aware ISO")
    return parsed.astimezone(timezone.utc).isoformat()


def _ledger_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("usage ledger timestamp is invalid")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    return _utc(datetime.fromisoformat(normalized))


def _ledger_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("usage ledger date is invalid")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("usage ledger date is invalid")
    return parsed


def parse_report_window(value: str, *, now: datetime) -> ReportWindow:
    """Parse the bounded whole-unit duration accepted by ``--since``."""
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError("--since must be a whole number of hours or days (maximum 30d)")
    count, unit = int(match.group(1)), match.group(2)
    if (unit == "h" and count > 720) or (unit == "d" and count > 30):
        raise ValueError("--since must be no longer than 30d")
    until = _utc(now)
    return ReportWindow(until - (timedelta(hours=count) if unit == "h" else timedelta(days=count)), until)


def _empty_amount() -> dict[str, int | None]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "embedding_items": 0,
        "rerank_documents": 0,
        "images": 0,
        "cost_microunits": 0,
    }


def _add_amount(target: dict[str, Any], row: sqlite3.Row, *, prefix: str = "") -> bool:
    for field in _AMOUNTS:
        target[field] = int(target[field]) + _ledger_int(row[f"{prefix}{field}"])
    target["total_tokens"] = int(target["input_tokens"]) + int(target["output_tokens"])
    cost = row[f"{prefix}cost_microunits"]
    unknown = cost is None or ("unknown_cost" in row.keys() and _ledger_flag(row["unknown_cost"]))
    if unknown:
        target["cost_microunits"] = None
    elif target["cost_microunits"] is not None:
        target["cost_microunits"] = int(target["cost_microunits"]) + _ledger_int(cost)
    return unknown


def _usage_identity(row: sqlite3.Row) -> UsageIdentity:
    capability = row["capability"]
    operation = row["operation"]
    plugin_id = row["plugin_id"]
    provider = row["provider"]
    model = row["model"]
    if not all(isinstance(value, str) for value in (capability, operation, plugin_id, provider, model)):
        raise ValueError("usage ledger labels must be strings")
    return UsageIdentity(
        ProviderCapability(capability),
        operation,
        plugin_id,
        provider,
        model,
    )


def _safe_error_category(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _SAFE_ERROR_CATEGORIES else "other"


def _ledger_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("usage ledger counter is invalid")
    return value


def _ledger_flag(value: object) -> bool:
    parsed = _ledger_int(value)
    if parsed not in (0, 1):
        raise ValueError("usage ledger flag is invalid")
    return bool(parsed)


def _ledger_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("usage ledger measurement is invalid")
    return float(value)


def _validate_event_row(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
    identity = _usage_identity(row)
    status = row["status"]
    if not isinstance(status, str) or _LABEL_PATTERN.fullmatch(status) is None:
        raise ValueError("usage ledger status is invalid")
    _ledger_date(row["usage_date"])
    _ledger_timestamp(row["created_at"])
    for field in ("id", "attempts", *_AMOUNTS):
        _ledger_int(row[field])
    cost = row["cost_microunits"]
    if cost is not None:
        _ledger_int(cost)
    _ledger_float(row["latency_ms"])
    _ledger_flag(row["unknown_outcome"])
    _ledger_flag(row["unknown_cost"])
    return identity.capability.value, identity.plugin_id, identity.provider, identity.model, status


def _validate_reservation_row(row: sqlite3.Row) -> tuple[str, date, datetime, datetime]:
    _usage_identity(row)
    state = row["state"]
    if not isinstance(state, str) or state not in _RESERVATION_STATES:
        raise ValueError("usage reservation state is invalid")
    usage_day = _ledger_date(row["usage_date"])
    created_at = _ledger_timestamp(row["created_at"])
    lease_expires_at = _ledger_timestamp(row["lease_expires_at"])
    if row["finalized_at"] is not None:
        _ledger_timestamp(row["finalized_at"])
    if state == "active":
        for field in ("attempts", *tuple(f"reserved_{name}" for name in _AMOUNTS)):
            _ledger_int(row[field])
        cost = row["reserved_cost_microunits"]
        if cost is not None:
            _ledger_int(cost)
    return state, usage_day, created_at, lease_expires_at


def _percentiles(samples: list[tuple[float, int]]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None}
    ordered = sorted(samples)
    return {
        "p50": ordered[math.ceil(0.5 * len(ordered)) - 1][0],
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1][0],
    }


def _utilization(limit: int, used: int | None) -> Decimal | None:
    return None if limit <= 0 or used is None else Decimal(used) / Decimal(limit)


class UsageLedgerReader:
    """Aggregate an existing UsageGovernor ledger without creating or changing it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise OpsReportError("usage ledger does not exist")
        quoted_path = quote(self.path.resolve().as_posix(), safe="/:")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{quoted_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            self._validate_schema(connection)
            return connection
        except OpsReportError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, OverflowError, TypeError, ValueError, sqlite3.Error):
            if connection is not None:
                connection.close()
            raise OpsReportError("usage ledger is unreadable") from None

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > USAGE_LEDGER_SCHEMA_VERSION:
            raise OpsReportError("usage ledger schema is newer than supported")
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version != USAGE_LEDGER_SCHEMA_VERSION or not {"usage_events", "usage_reservations"}.issubset(tables):
            raise OpsReportError("usage ledger has an unsupported schema")

    @staticmethod
    def _new_group(key: tuple[str, str, str, str, str]) -> dict[str, Any]:
        capability, plugin_id, provider, model, status = key
        return {
            "capability": capability,
            "plugin_id": plugin_id,
            "provider": provider,
            "model": model,
            "status": status,
            **_empty_amount(),
            "successes": 0,
            "errors": 0,
            "unknown_outcomes": 0,
            "unknown_costs": 0,
            "last_failure_at": None,
            "_latencies": [],
            "error_categories": defaultdict(int),
        }

    @staticmethod
    def _add_event(target: dict[str, Any], row: sqlite3.Row) -> None:
        unknown_cost = _add_amount(target, row)
        success = str(row["status"]) in _SUCCESS
        unknown_outcome = _ledger_flag(row["unknown_outcome"])
        created_at = _iso(row["created_at"])
        if success:
            target["successes"] += 1
        else:
            target["errors"] += 1
            failure_at = created_at
            if target["last_failure_at"] is None or failure_at > target["last_failure_at"]:
                target["last_failure_at"] = failure_at
            error_category = _safe_error_category(row["error_class"])
            if error_category is not None:
                target["error_categories"][error_category] += 1
        target["unknown_outcomes"] += int(unknown_outcome)
        target["unknown_costs"] += int(unknown_cost)
        target["_latencies"].append((_ledger_float(row["latency_ms"]), _ledger_int(row["id"])))

    @staticmethod
    def _finish(value: dict[str, Any]) -> dict[str, object]:
        value["latency_ms"] = _percentiles(value.pop("_latencies"))
        value["error_categories"] = dict(sorted(value["error_categories"].items()))
        return value

    def _reservations(
        self,
        connection: sqlite3.Connection,
        *,
        at: datetime,
        created_until: datetime | None = None,
        usage_day: date | None = None,
    ) -> dict[str, object]:
        cutoff = _utc(at)
        created_cutoff = _utc(created_until) if created_until is not None else None
        reserved: dict[str, Any] = _empty_amount()
        active_count = 0
        expired_count = 0
        for row in connection.execute("SELECT * FROM usage_reservations ORDER BY id"):
            state, row_day, created_at, lease_expires_at = _validate_reservation_row(row)
            if state != "active" or (created_cutoff is not None and created_at > created_cutoff):
                continue
            if usage_day is not None and row_day != usage_day:
                continue
            if lease_expires_at <= cutoff:
                expired_count += 1
            else:
                active_count += 1
                _add_amount(reserved, row, prefix="reserved_")
        return {
            "active_count": active_count,
            "expired_count": expired_count,
            "reserved": reserved,
        }

    @staticmethod
    def _daily_totals(connection: sqlite3.Connection, day: date) -> dict[str, Any]:
        totals: dict[str, Any] = _empty_amount()
        totals.update(errors=0, unknown_outcomes=0, unknown_costs=0)
        rows = connection.execute(
            "SELECT * FROM usage_events WHERE usage_date=? OR typeof(usage_date)<>'text' "
            "OR date(usage_date) IS NULL OR date(usage_date)<>usage_date ORDER BY id",
            (day.isoformat(),),
        )
        for row in rows:
            _validate_event_row(row)
            if _ledger_date(row["usage_date"]) != day:
                continue
            unknown_cost = _add_amount(totals, row)
            totals["errors"] += int(row["status"] not in _SUCCESS)
            totals["unknown_outcomes"] += int(_ledger_flag(row["unknown_outcome"]))
            totals["unknown_costs"] += int(unknown_cost)
        return totals

    @staticmethod
    def _budget(totals: dict[str, Any], reservations: dict[str, Any], limits: UsageLimits) -> dict[str, dict[str, Any]]:
        reserved = reservations["reserved"]
        assert isinstance(reserved, dict)
        requests = int(totals["requests"]) + int(reserved["requests"])
        tokens = int(totals["total_tokens"]) + int(reserved["total_tokens"])
        total_cost, reserved_cost = totals["cost_microunits"], reserved["cost_microunits"]
        cost = None if total_cost is None or reserved_cost is None else int(total_cost) + int(reserved_cost)
        return {
            "requests": {
                "limit": limits.daily_requests,
                "used": requests,
                "utilization": _utilization(limits.daily_requests, requests),
            },
            "tokens": {
                "limit": limits.daily_tokens,
                "used": tokens,
                "utilization": _utilization(limits.daily_tokens, tokens),
            },
            "cost_microunits": {
                "limit": limits.daily_cost_microunits,
                "used": cost,
                "utilization": _utilization(limits.daily_cost_microunits, cost),
            },
        }

    def report(self, window: ReportWindow, *, limits: UsageLimits) -> dict[str, object]:
        window = ReportWindow(_utc(window.since), _utc(window.until))
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM usage_events WHERE (created_at>=? AND created_at<=?) "
                "OR typeof(created_at)<>'text' OR julianday(created_at) IS NULL "
                "ORDER BY capability,plugin_id,provider,model,status,created_at,id",
                (_iso(window.since), _iso(window.until)),
            ).fetchall()
            groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
            totals: dict[str, Any] = self._new_group(("", "", "", "", ""))
            for row in rows:
                key = _validate_event_row(row)
                self._add_event(groups.setdefault(key, self._new_group(key)), row)
                self._add_event(totals, row)
            finished_groups = [self._finish(groups[key]) for key in sorted(groups)]
            finished_totals = self._finish(totals)
            for field in ("capability", "plugin_id", "provider", "model", "status"):
                finished_totals.pop(field)
            reservations = self._reservations(connection, at=window.until, created_until=window.until)
            return {
                "window": {"since": _iso(window.since), "until": _iso(window.until)},
                "groups": finished_groups,
                "totals": finished_totals,
                "reservations": reservations,
                "budget": self._budget(finished_totals, reservations, limits),
            }
        except OpsReportError:
            raise
        except (OverflowError, TypeError, ValueError, sqlite3.Error):
            raise OpsReportError("usage ledger is unreadable") from None
        finally:
            connection.close()

    def health_summary(self, *, day: date, limits: UsageLimits, now: datetime) -> dict[str, object]:
        current = _utc(now)
        connection = self._connect()
        try:
            totals = self._daily_totals(connection, day)
            reservations = cast(
                dict[str, Any],
                self._reservations(connection, at=current, created_until=current, usage_day=day),
            )
        except OpsReportError:
            raise
        except (OverflowError, TypeError, ValueError, sqlite3.Error):
            raise OpsReportError("usage ledger is unreadable") from None
        finally:
            connection.close()
        budget = self._budget(totals, reservations, limits)
        return {
            "failures": totals["errors"],
            "stale_reservations": reservations["expired_count"],
            "utilization": {name: value["utilization"] for name, value in budget.items()},
            "unknown_outcomes": totals["unknown_outcomes"],
            "unknown_costs": totals["unknown_costs"],
        }


def _report_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _file_size(path: Path, *, required: bool) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        if required:
            raise OpsReportError("main database does not exist") from None
        return 0
    except OSError as error:
        raise OpsReportError("database file metadata is unreadable") from error


def _usage_limits(settings: Settings) -> UsageLimits:
    return UsageLimits(
        daily_requests=settings.usage_daily_request_limit,
        daily_tokens=settings.daily_token_limit,
        daily_cost_microunits=settings.usage_daily_cost_limit_microunits,
    )


def _usage_snapshot(path: Path, *, window: ReportWindow, settings: Settings) -> tuple[dict[str, object], bool]:
    try:
        return UsageLedgerReader(path).report(window, limits=_usage_limits(settings)), False
    except OpsReportError as error:
        if str(error) != "usage ledger does not exist":
            raise
        return {
            "window": {"since": _iso(window.since), "until": _iso(window.until)},
            "groups": [],
            "totals": None,
            "reservations": None,
            "budget": None,
        }, True


def _worker_snapshot(
    worker_runtime: Mapping[str, object] | None,
    *,
    job_heartbeat: object,
    now: datetime,
    poll_interval: float,
) -> dict[str, object]:
    process_timestamps: list[str] = []
    if worker_runtime is not None:
        for key in (
            "heartbeat_at",
            "last_maintenance_completed_at",
            "last_maintenance_started_at",
            "started_at",
            "stopped_at",
        ):
            timestamp = _report_timestamp(worker_runtime.get(key))
            if timestamp is not None:
                process_timestamps.append(timestamp)
        if worker_runtime.get("running") is False:
            return {
                "state": "inactive",
                "source": "process",
                "heartbeat_at": max(process_timestamps) if process_timestamps else None,
            }
    heartbeat_at: str | None
    source: str | None
    if process_timestamps:
        heartbeat_at, source = max(process_timestamps), "process"
    else:
        heartbeat_at, source = _report_timestamp(job_heartbeat), "job_heartbeat"
    if heartbeat_at is None:
        return {"state": "unknown", "source": None, "heartbeat_at": None}
    age_seconds = max(
        0.0,
        (now.astimezone(timezone.utc) - datetime.fromisoformat(heartbeat_at)).total_seconds(),
    )
    state = "inactive" if age_seconds > 2 * poll_interval else "active"
    return {"state": state, "source": source, "heartbeat_at": heartbeat_at}


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def build_ops_report(
    connection: sqlite3.Connection,
    *,
    database_path: Path,
    usage_path: Path,
    settings: Settings,
    window: ReportWindow,
    now: datetime,
    worker_runtime: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assemble a stable, content-free operational report from read-only inputs."""
    current = _utc(now)
    window = ReportWindow(_utc(window.since), _utc(window.until))
    database_size = _file_size(database_path, required=True)
    file_sizes = {
        "database": {"size_bytes": database_size},
        "wal": {"size_bytes": _file_size(Path(f"{database_path}-wal"), required=False)},
        "shm": {"size_bytes": _file_size(Path(f"{database_path}-shm"), required=False)},
        "usage": {"size_bytes": _file_size(usage_path, required=False)},
    }

    connection.execute("BEGIN")
    try:
        from hl_mem.application.health import conflict_backlog_snapshot

        jobs = JobRepository(connection).report_snapshot(
            window,
            now=current,
            lease_seconds=settings.worker_job_lease_minutes * 60,
        )
        conflicts = conflict_backlog_snapshot(connection, now=current)
    finally:
        connection.rollback()

    usage, usage_unknown = _usage_snapshot(usage_path, window=window, settings=settings)
    worker = _worker_snapshot(
        worker_runtime,
        job_heartbeat=jobs["latest_heartbeat_at"],
        now=current,
        poll_interval=settings.worker_poll_interval,
    )
    warnings: set[str] = set()
    if usage_unknown:
        warnings.add("unknown_usage")
    else:
        reservations = cast(dict[str, object], usage["reservations"])
        expired_count = reservations["expired_count"]
        if not isinstance(expired_count, int):
            raise OpsReportError("usage ledger has an unsupported schema")
        if expired_count > 0:
            warnings.add("expired_reservation")
        budget = cast(dict[str, dict[str, object]], usage["budget"])
        if any(
            utilization is not None and Decimal(str(utilization)) >= Decimal("0.8")
            for value in budget.values()
            if (utilization := value["utilization"]) is not None
        ):
            warnings.add("budget_near_limit")
    if int(jobs["failed_count"]) + int(jobs["dead_count"]) > 0:
        warnings.add("failed_jobs")
    if int(jobs["expired_running_leases"]) > 0:
        warnings.add("stale_running_jobs")
    if worker["state"] == "inactive":
        warnings.add("worker_inactive")
    elif worker["state"] == "unknown":
        warnings.add("worker_unknown")
    if file_sizes["wal"]["size_bytes"] > max(database_size, 256 * 1024 * 1024):
        warnings.add("large_wal")
    report = {
        "schema_version": OPS_REPORT_SCHEMA_VERSION,
        "generated_at": _iso(current),
        "window": {"since": _iso(window.since), "until": _iso(window.until)},
        "usage": usage,
        "jobs": jobs,
        "conflicts": conflicts,
        "worker": worker,
        "files": file_sizes,
        "warnings": sorted(warnings),
    }
    return cast(dict[str, object], _json_value(report))
