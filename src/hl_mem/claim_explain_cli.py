"""Command-line interface for bounded Claim explanations."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from hl_mem.application.claim_explanation import explain_claim
from hl_mem.errors import NotFoundError
from hl_mem.observability.ops_cli import open_readonly_database
from hl_mem.settings import Settings


def add_claim_explain_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    explain = commands.add_parser("explain")
    explain_commands = explain.add_subparsers(dest="explain_command", required=True)
    claim = explain_commands.add_parser("claim")
    claim.add_argument("claim_id")
    claim.add_argument("--json", action="store_true")


def _print_claim_explanation(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return
    sections = (
        ("Claim", {"explanation_kind": result["explanation_kind"], **result["claim"]}),
        ("Provenance", result["provenance"]),
        ("Evidence", result["evidence"]),
        ("Limitations", result["limitations"]),
    )
    for title, value in sections:
        print(f"{title}:")
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def handle_claim_explain_command(args: argparse.Namespace, settings: Settings) -> bool:
    if args.command != "explain":
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = open_readonly_database(Path(settings.database_path))
        explanation = explain_claim(
            connection,
            args.claim_id,
            provenance_mode=settings.provenance_mode,
        )
    except (OSError, sqlite3.Error, NotFoundError, ValueError):
        print("claim explanation unavailable", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if connection is not None:
            connection.close()
    _print_claim_explanation(explanation, as_json=args.json)
    return True
