#!/usr/bin/env python
"""Validate and orchestrate the frozen v0.30 experiment manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from hl_mem.evaluation.v030_corpus import EXPERIMENTS, load_manifest, validate_manifest


def validate_manifest_directory(manifest_dir: str | Path) -> dict[str, object]:
    """Authenticate E1-E6 and summarize the frozen manifest set."""

    root = Path(manifest_dir)
    summaries: dict[str, dict[str, object]] = {}
    digests: dict[str, str] = {}
    for experiment in sorted(EXPERIMENTS):
        manifest = load_manifest(root / f"{experiment.lower()}.json")
        if manifest["experiment"] != experiment:
            raise ValueError(f"{experiment} manifest filename/content mismatch")
        summaries[experiment] = validate_manifest(manifest)
        digests[experiment] = str(manifest["manifest_sha256"])
    frozen_set = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "phase": "validate",
        "case_counts": {key: cast(int, value["case_count"]) for key, value in summaries.items()},
        "manifest_sha256": digests,
        "manifest_set_sha256": hashlib.sha256(frozen_set).hexdigest(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("validate",))
    parser.add_argument("--manifest-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = validate_manifest_directory(args.manifest_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
