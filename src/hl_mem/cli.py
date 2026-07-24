"""HL-Mem 管理命令行。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import __version__
from hl_mem.storage.database import Database, default_database_path
from hl_mem.storage.repository import EventRepository


def export_database(database_path: str | Path, output_path: str | Path) -> int:
    """将不可变事件按 JSONL 导出。"""
    rows = Database(database_path).open().execute("SELECT * FROM events ORDER BY recorded_at,id").fetchall()
    with Path(output_path).open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps({"type": "event", "data": dict(row)}, ensure_ascii=False) + "\n")
    return len(rows)


def import_database(database_path: str | Path, input_path: str | Path) -> int:
    """幂等导入 JSONL 事件档案。"""
    repository = EventRepository(Database(database_path).open())
    imported = 0
    with Path(input_path).open("r", encoding="utf-8") as stream:
        for line in stream:
            record: dict[str, Any] = json.loads(line)
            if record.get("type") != "event" or not isinstance(record.get("data"), dict):
                raise ValueError("archive contains unsupported record")
            imported += int(repository.insert_event(record["data"], commit=True))
    return imported


def list_conflicts(database_path: str | Path) -> list[dict[str, Any]]:
    """列出等待人工审核的冲突案例。"""
    database = Database(database_path)
    try:
        rows = database.open().execute(
            "SELECT * FROM conflict_cases WHERE status IN ('pending','manual_required') "
            "ORDER BY created_at,id"
        ).fetchall()
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
        return {"id": case_id, "status": status, "decision": decision, "resolved_at": resolved_at}
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
    args = parser.parse_args(argv)
    if args.command == "conflicts":
        result: Any = (
            list_conflicts(args.db)
            if args.conflict_command == "list"
            else resolve_conflict(args.db, args.case_id, args.decision)
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    count = export_database(args.db, args.path) if args.command == "export" else import_database(args.db, args.path)
    print(json.dumps({"processed": count}))


if __name__ == "__main__":
    main()
