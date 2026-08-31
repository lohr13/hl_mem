"""HL-Mem 管理命令行。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import __version__
from hl_mem.adapters.hermes.deployment import deploy_plugin, print_deployment_result
from hl_mem.application.conflict_backlog import (
    inspect_invalid_conflict_groups,
)
from hl_mem.application.conflict_backlog import repair_invalid_conflict_groups as apply_invalid_group_repair
from hl_mem.application.conflict_repairs import (
    inspect_dangling_conflicts,
)
from hl_mem.application.conflict_repairs import repair_dangling_conflicts as apply_dangling_conflict_repair
from hl_mem.application.conflicts import DEFAULT_HUMAN_RESOLVER, ResolutionService
from hl_mem.application.dedup_backlog import (
    drain_below_floor_pairs,
    inspect_below_floor_pairs,
)
from hl_mem.application.expired_cleanup import cleanup_expired_claims, inspect_expired_claims
from hl_mem.application.restore import restore_database
from hl_mem.application.version_report import report_version_cli
from hl_mem.components import make_embedder
from hl_mem.config.migrate import apply_config_migration, plan_config_migration
from hl_mem.config_loader import load_settings
from hl_mem.daily_cli import add_daily_commands, handle_daily_command
from hl_mem.doctor import main as doctor_main
from hl_mem.errors import ConflictError
from hl_mem.evaluation.runner import BenchmarkRunner
from hl_mem.settings import Settings
from hl_mem.storage.backup import backup_database, validate_backup
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.backfill_index_text import backfill_index_text

EXPORT_FORMAT_VERSION = "1"
IMPORT_BATCH_SIZE = 100
EVENT_ARCHIVE_COLUMNS = (
    "id",
    "idempotency_key",
    "tenant_id",
    "user_id",
    "project_id",
    "agent_id",
    "session_id",
    "event_type",
    "actor_type",
    "actor_id",
    "content_json",
    "occurred_at",
    "recorded_at",
    "source_uri",
    "content_hash",
    "sensitivity",
    "metadata_json",
)


class JSONLImportError(ValueError):
    """JSONL import failure carrying a machine-readable partial report."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _normalized_archive_event(event: dict[str, Any]) -> dict[str, Any]:
    """按 EventRepository 的存储规则规范化归档事件，用于安全判重。"""
    stored = dict(event)
    if "content" in stored:
        content_json = json.dumps(
            stored.pop("content"),
            ensure_ascii=False,
            sort_keys=True,
        )
        stored["content_json"] = content_json
        stored.setdefault(
            "content_hash",
            hashlib.sha256(content_json.encode()).hexdigest(),
        )
    if "metadata" in stored:
        stored["metadata_json"] = json.dumps(
            stored.pop("metadata"),
            ensure_ascii=False,
            sort_keys=True,
        )
    defaults = {
        "tenant_id": "default",
        "sensitivity": "normal",
    }
    return {column: stored[column] if column in stored else defaults.get(column) for column in EVENT_ARCHIVE_COLUMNS}


def _archive_event_matches_existing(connection: sqlite3.Connection, event: dict[str, Any]) -> bool:
    event_id = event.get("id")
    existing = connection.execute(
        f"SELECT {','.join(EVENT_ARCHIVE_COLUMNS)} FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    if existing is None:
        return False
    expected = _normalized_archive_event(event)
    return all(existing[column] == expected[column] for column in EVENT_ARCHIVE_COLUMNS)


def _open_readonly_database(database_path: Path) -> sqlite3.Connection:
    """以 SQLite 强制只读模式打开现有数据库，且不运行 migration。"""
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def export_database(
    database_path: str | Path,
    output_path: str | Path,
    *,
    settings: Settings | None = None,
) -> int:
    """将不可变事件按 JSONL 导出。"""
    database = Database(database_path, settings=settings)
    try:
        rows = database.open().execute("SELECT * FROM events ORDER BY recorded_at,id").fetchall()
    finally:
        database.close()
    with Path(output_path).open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "metadata", "format_version": EXPORT_FORMAT_VERSION}) + "\n")
        for row in rows:
            stream.write(json.dumps({"type": "event", "data": dict(row)}, ensure_ascii=False) + "\n")
    return len(rows)


def import_database(
    database_path: str | Path,
    input_path: str | Path,
    *,
    settings: Settings | None = None,
    skip_extraction_jobs: bool = False,
    batch_size: int = IMPORT_BATCH_SIZE,
) -> dict[str, Any]:
    """Import a JSONL event archive and atomically queue extraction jobs."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    report: dict[str, Any] = {
        "processed": 0,
        "events_created": 0,
        "events_skipped": 0,
        "jobs_queued": 0,
        "failed_batch": None,
        "claims_not_rebuilt": skip_extraction_jobs,
    }
    database = Database(database_path, settings=settings)
    connection = database.open()
    event_repository = EventRepository(connection)
    job_repository = JobRepository(connection)

    def fail(batch: int, line: int, error: Exception) -> JSONLImportError:
        message = str(error) or error.__class__.__name__
        report["failed_batch"] = {
            "batch": batch,
            "line": line,
            "error": message,
        }
        return JSONLImportError(f"JSONL import failed at batch {batch}, line {line}: {message}", dict(report))

    def import_batch(records: list[tuple[int, dict[str, Any]]], batch: int) -> None:
        created = 0
        skipped = 0
        queued = 0
        current_line = records[0][0]
        try:
            connection.execute("BEGIN IMMEDIATE")
            for current_line, event in records:
                event_id = event.get("id")
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("event data requires a non-empty string id")
                event_created = event_repository.insert_event(event, commit=False)
                if not event_created:
                    if not _archive_event_matches_existing(connection, event):
                        raise ValueError(f"event {event_id!r} conflicts with an existing event payload")
                    skipped += 1
                else:
                    created += 1
                if skip_extraction_jobs:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                job_created = job_repository.insert_job(
                    {
                        "id": uuid.uuid4().hex,
                        "job_type": "extract_event",
                        "payload": {"event_id": event_id},
                        "idempotency_key": f"extract:{event_id}",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                    commit=False,
                )
                if job_created:
                    queued += 1
                    continue
                existing_job = connection.execute(
                    "SELECT job_type,payload_json FROM jobs WHERE idempotency_key=?",
                    (f"extract:{event_id}",),
                ).fetchone()
                try:
                    existing_payload = json.loads(existing_job["payload_json"]) if existing_job is not None else None
                except json.JSONDecodeError:
                    existing_payload = None
                if (
                    existing_job is None
                    or existing_job["job_type"] != "extract_event"
                    or existing_payload != {"event_id": event_id}
                ):
                    raise ValueError(f"extraction job key conflict for event {event_id!r}")
            connection.commit()
        except Exception as error:
            connection.rollback()
            raise fail(batch, current_line, error) from error
        report["processed"] += len(records)
        report["events_created"] += created
        report["events_skipped"] += skipped
        report["jobs_queued"] += queued

    try:
        pending: list[tuple[int, dict[str, Any]]] = []
        batch = 1
        metadata_seen = False
        with Path(input_path).open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                try:
                    line = raw_line.decode("utf-8")
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise fail(batch, line_number, error) from error
                if not isinstance(record, dict):
                    raise fail(batch, line_number, ValueError("archive record must be a JSON object"))
                if record.get("type") == "metadata":
                    if metadata_seen or pending or report["processed"]:
                        raise fail(
                            batch,
                            line_number,
                            ValueError("archive metadata must appear exactly once before events"),
                        )
                    if record.get("format_version") != EXPORT_FORMAT_VERSION:
                        raise fail(
                            batch,
                            line_number,
                            ValueError(f"unsupported archive format version: {record.get('format_version')}"),
                        )
                    metadata_seen = True
                    continue
                if record.get("type") != "event" or not isinstance(record.get("data"), dict):
                    raise fail(batch, line_number, ValueError("archive contains unsupported record"))
                if not metadata_seen:
                    raise fail(
                        batch,
                        line_number,
                        ValueError("archive metadata must appear before events"),
                    )
                pending.append((line_number, record["data"]))
                if len(pending) == batch_size:
                    import_batch(pending, batch)
                    pending = []
                    batch += 1
        if not metadata_seen:
            raise fail(batch, 1, ValueError("archive metadata record is missing"))
        if pending:
            import_batch(pending, batch)
        return report
    finally:
        database.close()


def _summarize_claim_value(value: Any, limit: int = 160) -> str:
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return rendered if len(rendered) <= limit else f"{rendered[: limit - 3]}..."


def list_conflicts(
    database_path: str | Path,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """列出等待人工审核的冲突案例。"""
    database = Database(database_path, settings=settings)
    try:
        connection = database.open()
        rows = connection.execute(
            "SELECT * FROM conflict_cases WHERE status IN ('pending','manual_required') ORDER BY created_at,id"
        ).fetchall()
        repository = ClaimRepository(connection)
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for side in ("left", "right"):
                claim = repository.get_claim(item[f"{side}_claim_id"])
                item[f"{side}_value"] = _summarize_claim_value(claim.get("value")) if claim is not None else None
                item[f"{side}_status"] = claim.get("status") if claim is not None else None
                item[f"{side}_authority"] = claim.get("source_authority") if claim is not None else None
                item[f"{side}_recorded_from"] = claim.get("recorded_from") if claim is not None else None
            if item.get("group_key") is not None:
                item["candidates"] = ResolutionService(connection).review(str(item["id"]))["candidates"]
            result.append(item)
        return result
    finally:
        database.close()


def resolve_conflict(
    database_path: str | Path,
    case_id: str,
    decision: str,
    *,
    rationale: str | None = None,
    expected_revision: int | None = None,
    resolver: str = DEFAULT_HUMAN_RESOLVER,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """通过组级应用服务收敛指定冲突案例。"""
    database = Database(database_path, settings=settings)
    try:
        return ResolutionService(database.open()).resolve(
            case_id,
            decision,
            rationale=rationale,
            expected_revision=expected_revision,
            resolver=resolver,
        )
    finally:
        database.close()


def repair_dangling_conflicts(
    database_path: str | Path,
    *,
    apply: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Preview dangling conflict cases or repair the safe terminal subset."""
    database = Database(database_path, settings=settings)
    try:
        connection = database.open()
        cases = inspect_dangling_conflicts(connection)
        if apply:
            applied = apply_dangling_conflict_repair(connection, source="cli")
        else:
            applied = {"deleted_count": 0, "deleted_case_ids": []}
        return {"dry_run": not apply, "cases": cases, **applied}
    finally:
        database.close()


def repair_invalid_conflict_groups(
    database_path: str | Path,
    *,
    apply: bool = False,
    expected_count: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """默认只预览旧 ingest 形成的非互斥 open groups；apply 必须提供预期数量。"""
    database = Database(database_path, settings=settings)
    try:
        connection = database.open()
        preview = inspect_invalid_conflict_groups(connection)
        actual_count = int(preview["candidate_case_count"])
        if expected_count is not None and actual_count != expected_count:
            raise ConflictError(
                f"invalid conflict group count mismatch: expected {expected_count}, found {actual_count}"
            )
        if not apply:
            return {
                **preview,
                "dry_run": True,
                "applied_case_count": 0,
                "activated_claim_count": 0,
                "invalid_open_count": actual_count,
            }
        if expected_count is None:
            raise ConflictError("--expected-count is required with --apply")
        return apply_invalid_group_repair(
            connection,
            expected_count=expected_count,
            source="cli",
        )
    finally:
        database.close()


def drain_dedup_backlog(
    database_path: str | Path,
    *,
    apply: bool = False,
    expected_count: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Preview below-floor pending pairs or terminally classify the exact expected set."""
    resolved_settings = settings or Settings()
    threshold = resolved_settings.dedup_threshold
    if apply and expected_count is None:
        raise ConflictError("--expected-count is required with --apply")
    if not apply:
        connection = _open_readonly_database(Path(database_path))
        try:
            preview = inspect_below_floor_pairs(connection, threshold=threshold)
        finally:
            connection.close()
        actual_count = int(preview["candidate_pair_count"])
        if expected_count is not None and actual_count != expected_count:
            raise ConflictError(f"dedup below-floor count mismatch: expected {expected_count}, found {actual_count}")
        return {
            **preview,
            "dry_run": True,
            "applied_pair_count": 0,
            "remaining_below_floor_count": actual_count,
            "claim_rows_updated": 0,
        }
    database = Database(database_path, settings=resolved_settings)
    try:
        assert expected_count is not None
        return drain_below_floor_pairs(
            database.open(),
            threshold=threshold,
            expected_count=expected_count,
            source="cli",
        )
    finally:
        database.close()


def cleanup_expired_history(
    database_path: str | Path,
    *,
    apply: bool = False,
    expected_count: int | None = None,
    limit: int | None = None,
    now: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Preview eligible expired history or reclaim one exact, bounded batch."""
    resolved_settings = settings or Settings()
    reference = now or datetime.now(timezone.utc).isoformat()
    retention_days = resolved_settings.expired_claim_retention_days
    batch_size = limit or resolved_settings.expired_cleanup_batch_size
    if batch_size < 1:
        raise ValueError("--limit must be positive")
    if apply and expected_count is None:
        raise ConflictError("--expected-count is required with --apply")
    if not apply:
        connection = _open_readonly_database(Path(database_path))
        try:
            preview = inspect_expired_claims(
                connection,
                now=reference,
                retention_days=retention_days,
            )
        finally:
            connection.close()
        actual_count = int(preview["eligible_claim_count"])
        if expected_count is not None and actual_count != expected_count:
            raise ConflictError(f"expired cleanup count mismatch: expected {expected_count}, found {actual_count}")
        return {
            **preview,
            "dry_run": True,
            "batch_size": batch_size,
            "deleted": 0,
            "remaining_eligible_count": actual_count,
        }
    database = Database(database_path, settings=resolved_settings)
    try:
        assert expected_count is not None
        return cleanup_expired_claims(
            database.open(),
            now=reference,
            retention_days=retention_days,
            batch_size=batch_size,
            expected_count=expected_count,
            source="cli",
        )
    finally:
        database.close()


def _add_conflicts_parser(commands: Any) -> None:
    conflicts = commands.add_parser("conflicts")
    conflicts.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    conflict_commands = conflicts.add_subparsers(dest="conflict_command", required=True)
    conflict_commands.add_parser("list")
    resolve = conflict_commands.add_parser("resolve")
    resolve.add_argument("case_id")
    resolve.add_argument("decision", choices=("keep_left", "keep_right", "coexist", "reject"))
    resolve.add_argument("--rationale")
    resolve.add_argument("--expected-revision", type=int, required=True)
    resolve.add_argument("--resolver", default=DEFAULT_HUMAN_RESOLVER)
    repair_dangling = conflict_commands.add_parser("repair-dangling")
    repair_dangling.add_argument("--apply", action="store_true")
    repair_invalid = conflict_commands.add_parser("repair-invalid-groups")
    repair_invalid.add_argument("--apply", action="store_true")
    repair_invalid.add_argument("--expected-count", type=int)


def main(argv: Sequence[str] | None = None) -> None:
    """运行导入或导出管理命令。"""
    parser = argparse.ArgumentParser(prog="hl-mem")
    parser.add_argument("--version", action="version", version=f"hl_mem {__version__}")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("path", type=Path)
    export.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    import_archive = commands.add_parser("import")
    import_archive.add_argument("path", type=Path)
    import_archive.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    import_archive.add_argument(
        "--skip-extraction-jobs",
        action="store_true",
        help="forensic restore only; imported events will not rebuild claims",
    )
    backup = commands.add_parser("backup")
    backup.add_argument("path", type=Path)
    backup.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    restore = commands.add_parser(
        "restore",
        description=(
            "Restore a verified backup after replaying <target>.tombstones.db. "
            "Stop the API, workers, and all writers first."
        ),
    )
    restore.add_argument("path", type=Path)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    restore.add_argument("--confirm-overwrite", action="store_true")
    _add_conflicts_parser(commands)
    dedup = commands.add_parser("dedup")
    dedup.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    dedup_commands = dedup.add_subparsers(dest="dedup_command", required=True)
    drain_below_floor = dedup_commands.add_parser("drain-below-floor")
    drain_below_floor.add_argument("--apply", action="store_true")
    drain_below_floor.add_argument("--expected-count", type=int)
    expired = commands.add_parser("expired")
    expired.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    expired_commands = expired.add_subparsers(dest="expired_command", required=True)
    cleanup_expired = expired_commands.add_parser("cleanup")
    cleanup_expired.add_argument("--apply", action="store_true")
    cleanup_expired.add_argument("--expected-count", type=int)
    cleanup_expired.add_argument("--limit", type=int)
    cleanup_expired.add_argument("--now", help=argparse.SUPPRESS)
    evaluation = commands.add_parser("eval")
    evaluation.add_argument("--benchmark", choices=("longmemeval",), default="longmemeval")
    evaluation.add_argument("--subset", default="core")
    evaluation.add_argument("--source", type=Path, required=True)
    evaluation.add_argument("--output", type=Path, required=True)
    evaluation.add_argument(
        "--layers",
        default="extraction,retrieval,lifecycle",
        help="逗号分隔的 extraction,retrieval,lifecycle",
    )
    evaluation.add_argument("--limit", type=int)
    evaluation.add_argument("--keep-db", action="store_true")
    version_report = commands.add_parser("report-version")
    version_report.add_argument("--namespace", default="default")
    version_report.add_argument("--subject", required=True)
    backfill = commands.add_parser("backfill-index-text")
    backfill.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--cursor")
    backfill.add_argument("--mode", choices=("legacy", "value_only", "natural", "answerable"))
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    doctor.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    doctor.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_migrate = config_commands.add_parser("migrate")
    config_migrate.add_argument("--config", type=Path, required=True)
    config_migrate.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    config_migrate.add_argument("--apply", action="store_true")
    config_migrate.add_argument("--backup", type=Path)
    config_migrate.add_argument("--manifest", type=Path)
    hermes = commands.add_parser("hermes", help="安装或升级 Hermes 插件")
    hermes_commands = hermes.add_subparsers(dest="hermes_command", required=True)
    for action in ("install", "upgrade"):
        deployment = hermes_commands.add_parser(action)
        deployment.add_argument("--hermes-home", type=Path)
        deployment.add_argument("--dry-run", action="store_true")
    add_daily_commands(commands)
    args = parser.parse_args(argv)
    if args.command == "config":
        plan = plan_config_migration(args.config, env_path=args.env_file)
        report: dict[str, Any] = {
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
        return
    if handle_daily_command(args, parser):
        return
    if args.command == "hermes":
        try:
            print_deployment_result(
                deploy_plugin(
                    args.hermes_command,
                    args.hermes_home,
                    dry_run=args.dry_run,
                )
            )
        except (OSError, RuntimeError) as error:
            print(f"Hermes plugin {args.hermes_command} failed: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        return
    if args.command == "doctor":
        doctor_args: list[str] = []
        if args.db is not None:
            doctor_args.extend(["--db", str(args.db)])
        if args.config is not None:
            doctor_args.extend(["--config", str(args.config)])
        if args.env_file is not None:
            doctor_args.extend(["--env-file", str(args.env_file)])
        raise SystemExit(doctor_main(doctor_args))
    settings = load_settings(args.config, args.env_file)
    if args.db is not None:
        settings = replace(settings, database_path=str(args.db))
    if args.command == "report-version":
        return print(report_version_cli(settings, args.namespace, args.subject))
    if args.command == "server":
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        from hl_mem.server import run_server

        run_server(settings, host=args.host, port=args.port)
        return
    database_path = Path(settings.database_path)
    if args.command == "backup":
        manifest = backup_database(database_path, args.path)
        print(json.dumps(validate_backup(args.path, manifest), ensure_ascii=False, sort_keys=True))
        return
    if args.command == "restore":
        restored = restore_database(
            args.path,
            args.manifest,
            database_path,
            confirm_overwrite=args.confirm_overwrite,
        )
        print(json.dumps(restored, ensure_ascii=False, sort_keys=True))
        return
    if args.command == "backfill-index-text":
        database: Database | None = None
        connection: sqlite3.Connection | None = None
        try:
            if args.dry_run:
                connection = _open_readonly_database(database_path)
            else:
                database = Database(settings=settings)
                connection = database.open()
            result = backfill_index_text(
                connection,
                make_embedder(settings),
                mode=args.mode or settings.index_text_mode,
                version=settings.index_text_version,
                batch_size=settings.index_backfill_batch_size,
                max_attempts=settings.index_backfill_max_attempts,
                dry_run=args.dry_run,
                cursor=args.cursor,
            )
        finally:
            if database is None:
                if connection is not None:
                    connection.close()
            else:
                database.close()
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        if result.failed > 0 or not result.coverage_complete:
            raise SystemExit(1)
        return
    if args.command == "eval":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be positive")
        layers = tuple(item.strip() for item in args.layers.split(",") if item.strip())
        benchmark_result = BenchmarkRunner(settings=settings, limit=args.limit).run(
            source=args.source,
            subset=args.subset,
            layers=layers,
            output=args.output,
            keep_db=args.keep_db,
        )
        print(
            json.dumps(
                {
                    "benchmark": args.benchmark,
                    "cases": len(benchmark_result["cases"]),
                    "config_hash": benchmark_result["config_hash"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    if args.command == "dedup":
        dedup_result = drain_dedup_backlog(
            database_path,
            apply=args.apply,
            expected_count=args.expected_count,
            settings=settings,
        )
        print(json.dumps(dedup_result, ensure_ascii=False, sort_keys=True))
        return
    if args.command == "expired":
        expired_result = cleanup_expired_history(
            database_path,
            apply=args.apply,
            expected_count=args.expected_count,
            limit=args.limit,
            now=args.now,
            settings=settings,
        )
        print(json.dumps(expired_result, ensure_ascii=False, sort_keys=True))
        return
    if args.command == "conflicts":
        if args.conflict_command == "list":
            conflict_result: Any = list_conflicts(database_path, settings=settings)
        elif args.conflict_command == "resolve":
            conflict_result = resolve_conflict(
                database_path,
                args.case_id,
                args.decision,
                rationale=args.rationale,
                expected_revision=args.expected_revision,
                resolver=args.resolver,
                settings=settings,
            )
        elif args.conflict_command == "repair-dangling":
            conflict_result = repair_dangling_conflicts(
                database_path,
                apply=args.apply,
                settings=settings,
            )
        else:
            conflict_result = repair_invalid_conflict_groups(
                database_path,
                apply=args.apply,
                expected_count=args.expected_count,
                settings=settings,
            )
        print(json.dumps(conflict_result, ensure_ascii=False, sort_keys=True))
        return
    if args.command == "export":
        print(json.dumps({"processed": export_database(database_path, args.path, settings=settings)}))
        return
    try:
        import_report = import_database(
            database_path,
            args.path,
            settings=settings,
            skip_extraction_jobs=args.skip_extraction_jobs,
        )
    except JSONLImportError as error:
        print(json.dumps(error.report, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1) from error
    if import_report["jobs_queued"] > 0:
        print(
            f"Warning: {import_report['jobs_queued']} extraction job(s) queued; workers may invoke "
            "their currently configured extractor model when processing them.",
            file=sys.stderr,
        )
    print(json.dumps(import_report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
