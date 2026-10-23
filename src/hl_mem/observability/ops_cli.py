"""Command-line presentation and execution for operational reports."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from hl_mem.errors import OpsReportError
from hl_mem.observability.ops_report import build_ops_report, parse_report_window
from hl_mem.observability.usage_types import default_usage_ledger_path
from hl_mem.settings import Settings
from hl_mem.storage.database import known_migration_versions

_MAX_HEALTH_RESPONSE_BYTES = 64 * 1024


def open_readonly_database(database_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without running migrations or permitting writes."""
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _print_report(report: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return
    sections = (
        ("Summary", {key: report[key] for key in ("schema_version", "generated_at", "window")}),
        ("Providers", report["usage"]),
        ("Jobs", report["jobs"]),
        ("Worker", report["worker"]),
        ("Storage", report["files"]),
        ("Conflicts", report["conflicts"]),
        ("Warnings", report["warnings"]),
    )
    for title, value in sections:
        print(f"{title}:")
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _validate_database(connection: sqlite3.Connection) -> None:
    tables = {str(row["name"]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required_tables = {"schema_migrations", "jobs", "conflict_cases"}
    if not required_tables.issubset(tables):
        raise OpsReportError("main database has an unsupported schema")
    applied = {str(row["version"]) for row in connection.execute("SELECT version FROM schema_migrations")}
    if not applied.issubset(known_migration_versions()):
        raise OpsReportError("main database schema is newer than supported")


def _fetch_worker_runtime(settings: Settings) -> dict[str, object] | None:
    """Read the configured daemon's bounded health snapshot when Hermes uses it."""
    if not settings.hermes_enabled:
        return None
    try:
        with urlopen(f"{settings.hermes_url.rstrip('/')}/healthz", timeout=1.0) as response:
            raw = response.read(_MAX_HEALTH_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_HEALTH_RESPONSE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    monitoring = payload.get("monitoring")
    if not isinstance(monitoring, dict):
        return None
    worker = monitoring.get("worker")
    return worker if isinstance(worker, dict) else None


def _run_report(
    settings: Settings,
    *,
    since: str,
    as_json: bool,
    parser: argparse.ArgumentParser,
) -> None:
    try:
        window = parse_report_window(since, now=datetime.now(timezone.utc))
    except ValueError as error:
        parser.error(str(error))
    database_path = Path(settings.database_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = open_readonly_database(database_path)
        _validate_database(connection)
        report = build_ops_report(
            connection,
            database_path=database_path,
            usage_path=default_usage_ledger_path(database_path),
            settings=settings,
            window=window,
            now=window.until,
            worker_runtime=_fetch_worker_runtime(settings),
        )
    except (OSError, sqlite3.Error, OpsReportError):
        print("ops report unavailable", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if connection is not None:
            connection.close()
    _print_report(report, as_json=as_json)


def add_ops_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ops = commands.add_parser("ops")
    ops_commands = ops.add_subparsers(dest="ops_command", required=True)
    report = ops_commands.add_parser("report")
    report.add_argument("--since", default="24h")
    report.add_argument("--json", action="store_true")


def handle_ops_command(
    args: argparse.Namespace,
    settings: Settings,
    parser: argparse.ArgumentParser,
) -> bool:
    if args.command != "ops":
        return False
    _run_report(settings, since=args.since, as_json=args.json, parser=parser)
    return True
