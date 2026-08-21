#!/usr/bin/env python
"""Generate the frozen state corpus from privacy-safe structural seeds."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from hl_mem.evaluation.state_counterexample_corpus import generate_corpus, load_redacted_seeds


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-source", type=Path, required=True, help="JSONL produced by sample_state_events.py")
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)

    manifest = generate_corpus(load_redacted_seeds(arguments.seed_source), arguments.output_dir)
    print(
        json.dumps(
            {
                "bundles": manifest["totals"]["bundles"],
                "events": manifest["totals"]["events"],
                "dev_bundles": manifest["splits"]["dev"]["bundles"],
                "sealed_bundles": manifest["splits"]["sealed"]["bundles"],
                "manifest": str((arguments.output_dir / "v0300_state_corpus_manifest.json").resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
