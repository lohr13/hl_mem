#!/usr/bin/env python
"""Generate and validate the stable, content-free operational-report schema."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from hl_mem.observability.ops_report import ReportWindow, build_ops_report
from hl_mem.observability.usage import UsageAmount, UsageGovernor, UsageIdentity, UsageLimits
from hl_mem.observability.usage_types import default_usage_ledger_path
from hl_mem.plugins.contracts import ProviderCapability
from hl_mem.settings import Settings
from hl_mem.storage.database import Database

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "docs" / "ops-report.schema.json"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def build_schema() -> dict[str, object]:
    """Return the versioned public envelope for aggregate-only operational data."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "HL-Mem operational report",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "generated_at",
            "window",
            "usage",
            "jobs",
            "conflicts",
            "worker",
            "files",
            "warnings",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "generated_at": {"type": "string"},
            "window": {
                "type": "object",
                "additionalProperties": False,
                "required": ["since", "until"],
                "properties": {"since": {"type": "string"}, "until": {"type": "string"}},
            },
            "usage": {"type": "object"},
            "jobs": {"type": "object"},
            "conflicts": {"type": "object"},
            "worker": {"type": "object"},
            "files": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def _report(*, seeded: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database_path = root / "memory.db"
        settings = Settings.for_test()
        database = Database(database_path, settings=settings)
        database.open()
        database.close()
        usage_path = default_usage_ledger_path(database_path)
        if seeded:
            governor = UsageGovernor(
                usage_path,
                UsageLimits(daily_requests=10, daily_tokens=100, daily_cost_microunits=1_000),
                now=lambda: NOW,
            )
            reservation = governor.reserve(
                UsageIdentity(ProviderCapability.LLM, "extract", "builtin", "provider", "model"),
                UsageAmount(requests=1, input_tokens=2, cost_microunits=3),
            )
            governor.mark_attempt(reservation.id)
            governor.settle(
                reservation.id,
                UsageAmount(requests=1, input_tokens=2, cost_microunits=3),
                status="success",
                latency_ms=1,
            )
        connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            return build_ops_report(
                connection,
                database_path=database_path,
                usage_path=usage_path,
                settings=settings,
                window=ReportWindow(NOW - timedelta(hours=24), NOW),
                now=NOW,
            )
        finally:
            connection.close()


def _validate(schema: dict[str, object], report: dict[str, object]) -> None:
    Draft202012Validator(schema).validate(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    generated = build_schema()
    if args.write:
        SCHEMA_PATH.write_text(
            json.dumps(generated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not SCHEMA_PATH.is_file():
        print("ops report schema missing; run scripts/check_ops_report_schema.py --write")
        return 1
    try:
        committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        if committed != generated:
            print("ops report schema mismatch; run scripts/check_ops_report_schema.py --write")
            return 1
        _validate(committed, _report(seeded=False))
        seeded_report = _report(seeded=True)
        _validate(committed, seeded_report)
        try:
            _validate(committed, {**seeded_report, "unexpected": True})
        except ValidationError:
            pass
        else:
            print("ops report schema must reject unknown top-level fields")
            return 1
    except (OSError, ValueError, sqlite3.Error, ValidationError) as error:
        print(f"ops report schema check failed: {error}")
        return 1
    print("ops report schema check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
