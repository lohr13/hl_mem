"""CLI surface for bounded operational-model history repair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from hl_mem.application.model_coordinate_repair import (
    apply_model_coordinate_history_repair,
    inspect_model_coordinate_history,
)
from hl_mem.errors import ConflictError
from hl_mem.observability.ops_cli import open_readonly_database
from hl_mem.settings import Settings
from hl_mem.storage.database import Database


def add_model_coordinate_repair_command(commands: Any) -> None:
    coordinates = commands.add_parser("coordinates", help="Inspect or repair typed Claim coordinates")
    subcommands = coordinates.add_subparsers(dest="coordinate_command", required=True)
    repair = subcommands.add_parser("repair-model-history")
    repair.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    repair.add_argument("--namespace", default="default")
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--expected-count", type=int)


def handle_model_coordinate_repair_command(args: Any, settings: Settings) -> bool:
    if getattr(args, "command", None) != "coordinates":
        return False
    expected_count = getattr(args, "expected_count", None)
    if args.apply and expected_count is None:
        raise ConflictError("--expected-count is required with --apply")
    if not args.apply:
        connection = open_readonly_database(Path(settings.database_path))
        try:
            result = inspect_model_coordinate_history(connection, namespace=args.namespace)
        finally:
            connection.close()
    else:
        assert expected_count is not None
        database = Database(settings=settings)
        try:
            result = apply_model_coordinate_history_repair(
                database.open(),
                expected_count=expected_count,
                namespace=args.namespace,
            )
        finally:
            database.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return True
