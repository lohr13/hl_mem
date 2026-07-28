"""HL-Mem 管理命令行。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import __version__
from hl_mem.components import make_embedder
from hl_mem.evaluation.runner import BenchmarkRunner
from hl_mem.settings import Settings
from hl_mem.storage.database import Database, default_database_path
from hl_mem.storage.events import EventRepository
from hl_mem.workers.backfill_index_text import backfill_index_text

EXPORT_FORMAT_VERSION = "1"


def export_database(database_path: str | Path, output_path: str | Path) -> int:
    """将不可变事件按 JSONL 导出。"""
    database = Database(database_path)
    try:
        rows = database.open().execute("SELECT * FROM events ORDER BY recorded_at,id").fetchall()
    finally:
        database.close()
    with Path(output_path).open("w", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "metadata", "format_version": EXPORT_FORMAT_VERSION}) + "\n")
        for row in rows:
            stream.write(json.dumps({"type": "event", "data": dict(row)}, ensure_ascii=False) + "\n")
    return len(rows)


def import_database(database_path: str | Path, input_path: str | Path) -> int:
    """幂等导入 JSONL 事件档案。"""
    database = Database(database_path)
    try:
        repository = EventRepository(database.open())
        imported = 0
        with Path(input_path).open("r", encoding="utf-8") as stream:
            for line in stream:
                record: dict[str, Any] = json.loads(line)
                if record.get("type") == "metadata":
                    if record.get("format_version") != EXPORT_FORMAT_VERSION:
                        raise ValueError(f"unsupported archive format version: {record.get('format_version')}")
                    continue
                if record.get("type") != "event" or not isinstance(record.get("data"), dict):
                    raise ValueError("archive contains unsupported record")
                imported += int(repository.insert_event(record["data"], commit=True))
        return imported
    finally:
        database.close()


def list_conflicts(database_path: str | Path) -> list[dict[str, Any]]:
    """列出等待人工审核的冲突案例。"""
    database = Database(database_path)
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


def resolve_conflict(database_path: str | Path, case_id: str, decision: str) -> dict[str, Any]:
    """按人工决策收敛指定冲突案例。"""
    database = Database(database_path)
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
    parser.add_argument("--db", type=Path, default=default_database_path())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "import"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--db", type=Path, default=argparse.SUPPRESS)
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
    args = parser.parse_args(argv)
    if args.command == "backfill-index-text":
        settings = Settings.from_env()
        database = Database(args.db)
        try:
            result = backfill_index_text(
                database.open(),
                make_embedder(settings),
                mode=settings.index_text_mode,
                version=settings.index_text_version,
                batch_size=settings.index_backfill_batch_size,
                max_attempts=settings.index_backfill_max_attempts,
                dry_run=args.dry_run,
                cursor=args.cursor,
            )
        finally:
            database.close()
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return
    if args.command == "eval":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be positive")
        layers = tuple(item.strip() for item in args.layers.split(",") if item.strip())
        benchmark_result = BenchmarkRunner(limit=args.limit).run(
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
            list_conflicts(args.db)
            if args.conflict_command == "list"
            else resolve_conflict(args.db, args.case_id, args.decision)
        )
        print(json.dumps(conflict_result, ensure_ascii=False, sort_keys=True))
        return
    count = export_database(args.db, args.path) if args.command == "export" else import_database(args.db, args.path)
    print(json.dumps({"processed": count}))


if __name__ == "__main__":
    main()
