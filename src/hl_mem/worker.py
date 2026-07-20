from __future__ import annotations

import argparse
import json
import os

from hl_mem.storage.database import Database
from hl_mem.storage.repository import JobRepository
from hl_mem.workers.worker import Worker


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m hl_mem.worker")
    parser.add_argument("command", choices=("run", "run-once", "status"))
    parser.add_argument("--db", default=os.getenv("HL_MEM_DB_PATH", "hl_mem.db"))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()
    if args.command == "status":
        database = Database(args.db)
        try:
            print(json.dumps(JobRepository(database.open()).counts(), sort_keys=True))
        finally:
            database.close()
        return
    worker = Worker(args.db)
    if args.command == "run-once":
        try:
            print(json.dumps(worker.run_once(), ensure_ascii=False, sort_keys=True))
        finally:
            worker.database.close()
    else:
        worker.run_forever(args.poll_interval)


if __name__ == "__main__":
    main()
