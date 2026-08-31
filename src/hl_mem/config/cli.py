"""Configuration-specific CLI registration and migration reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hl_mem.config.migrate import apply_config_migration, plan_config_migration


def add_config_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    migrate = config_commands.add_parser("migrate")
    migrate.add_argument("--config", type=Path, required=True)
    migrate.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--backup", type=Path)
    migrate.add_argument("--manifest", type=Path)


def handle_config_command(args: argparse.Namespace) -> bool:
    if args.command != "config":
        return False
    plan = plan_config_migration(args.config, env_path=args.env_file)
    report: dict[str, object] = {
        "blockers": list(plan.blockers),
        "changes": [
            {
                "after": change.after,
                "before": change.before,
                "path": change.path,
                "reason": change.reason,
            }
            for change in plan.changes
        ],
        "dry_run": not args.apply,
        "recovery_required": plan.recovery_required,
        "removed": list(plan.removed),
        "source": str(plan.source),
        "source_version": plan.source_version,
        "status": "blocked" if plan.blockers else "ready",
        "target_version": plan.target_version,
    }
    if args.apply:
        config_backup = apply_config_migration(
            plan,
            backup_path=args.backup,
            manifest_path=args.manifest,
            env_path=args.env_file,
        )
        report["config_backup"] = str(config_backup)
        report["status"] = "applied"
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if plan.blockers:
        raise SystemExit(2)
    return True


__all__ = ["add_config_command", "handle_config_command"]
