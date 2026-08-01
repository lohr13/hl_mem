"""Atomically rebuild one pre-tokenized FTS v2 channel."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.storage.tokenized_fts import CHANNELS, backfill_tokenized_fts


def main(argv: Sequence[str] | None = None) -> None:
    """Run a full rebuild for the selected tokenized FTS v2 channel."""
    parser = argparse.ArgumentParser(prog="python -m hl_mem.workers.backfill_tokenized_fts")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--channel", required=True, choices=CHANNELS)
    args = parser.parse_args(argv)

    database = Database(args.db)
    try:
        count = backfill_tokenized_fts(database.open(), args.channel)
        print(json.dumps({"backfilled": count, "channel": args.channel}, sort_keys=True))
    finally:
        database.close()


if __name__ == "__main__":
    main()
