"""HL-Mem 管理命令行。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import __version__
from hl_mem.components import make_embedder
from hl_mem.config_loader import load_settings
from hl_mem.doctor import main as doctor_main
from hl_mem.evaluation.runner import BenchmarkRunner
from hl_mem.settings import Settings
from hl_mem.storage.backup import backup_database, restore_database, validate_backup
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


def list_conflicts(
    database_path: str | Path,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """列出等待人工审核的冲突案例。"""
    database = Database(database_path, settings=settings)
    try:
        rows = (
            database.open()
            .execute(
                "SELECT * FROM conflict_cases WHERE status IN ('pending','manual_required') " "ORDER BY created_at,id"
            )
            .fetchall()
        )
        return [dict(row) for row in rows]
    finally:
        database.close()


def resolve_conflict(
    database_path: str | Path,
    case_id: str,
    decision: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """按人工决策收敛指定冲突案例。"""
    database = Database(database_path, settings=settings)
    connection = database.open()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM conflict_cases WHERE id=? AND status IN ('pending','manual_required')",
            (case_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"open conflict case not found: {case_id}")
        case = dict(row)
        if decision in {"keep_left", "keep_right"}:
            winner_side = decision.removeprefix("keep_")
            winner_id = case[f"{winner_side}_claim_id"]
            connection.execute(
                "UPDATE claims SET status='active' WHERE id=? AND status IN ('candidate','disputed')",
                (winner_id,),
            )
            status = "resolved"
        elif decision == "coexist":
            connection.execute(
                "UPDATE claims SET status='active' WHERE id IN (?,?) AND status IN ('candidate','disputed')",
                (case["left_claim_id"], case["right_claim_id"]),
            )
            status = "resolved"
        else:
            status = "rejected"
        resolved_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE conflict_cases SET status=?,decision=?,resolved_at=? WHERE id=?",
            (status, decision, resolved_at, case_id),
        )
        connection.commit()
        return {
            "id": case_id,
            "status": status,
            "decision": decision,
            "resolved_at": resolved_at,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        database.close()


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
        description="Restore a verified backup. Stop the API, workers, and all writers first.",
    )
    restore.add_argument("path", type=Path)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    restore.add_argument("--confirm-overwrite", action="store_true")
    conflicts = commands.add_parser("conflicts")
    conflicts.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    conflict_commands = conflicts.add_subparsers(dest="conflict_command", required=True)
    conflict_commands.add_parser("list")
    resolve = conflict_commands.add_parser("resolve")
    resolve.add_argument("case_id")
    resolve.add_argument("decision", choices=("keep_left", "keep_right", "coexist", "reject"))
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
    backfill = commands.add_parser("backfill-index-text")
    backfill.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--cursor")
    backfill.add_argument("--mode", choices=("legacy", "answerable"))
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    doctor.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    doctor.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
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
    if args.command == "conflicts":
        conflict_result: Any = (
            list_conflicts(database_path, settings=settings)
            if args.conflict_command == "list"
            else resolve_conflict(
                database_path,
                args.case_id,
                args.decision,
                settings=settings,
            )
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
    print(json.dumps(import_report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
