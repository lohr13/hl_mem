"""HL-Mem 管理命令行。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

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
            imported += int(repository.insert_event(record["data"]))
    return imported


def main() -> None:
    """运行导入或导出管理命令。"""
    parser = argparse.ArgumentParser(prog="hl-mem")
    parser.add_argument("command", choices=("export", "import"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--db", type=Path, default=default_database_path())
    args = parser.parse_args()
    count = export_database(args.db, args.path) if args.command == "export" else import_database(args.db, args.path)
    print(json.dumps({"processed": count}))


if __name__ == "__main__":
    main()
