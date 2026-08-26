#!/usr/bin/env python
"""Sample irreversible event structures from an explicitly supplied SQLite source."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from hl_mem.evaluation.state_counterexample_corpus import sample_redacted_seeds, write_redacted_seeds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True, help="existing SQLite events database; opened mode=ro")
    parser.add_argument("--output", type=Path, required=True, help="destination JSONL for irreversible seed structures")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--recorded-after")
    parser.add_argument("--recorded-before")
    parser.add_argument("--seed", default="v0300-state-counterexamples-v1")
    arguments = parser.parse_args(argv)

    seeds = sample_redacted_seeds(
        arguments.source_db,
        limit=arguments.limit,
        recorded_after=arguments.recorded_after,
        recorded_before=arguments.recorded_before,
        seed=arguments.seed,
    )
    write_redacted_seeds(arguments.output, seeds)
    print(
        json.dumps(
            {"source_mode": "sqlite-readonly", "seeds": len(seeds), "output": str(arguments.output.resolve())},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
