#!/usr/bin/env python
"""Generate or verify the frozen Provider usage pricing schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hl_mem.observability.pricing import build_usage_pricing_schema

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "docs" / "usage-pricing.schema.json"


def rendered_schema() -> str:
    return json.dumps(build_usage_pricing_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()
    if args.write:
        SNAPSHOT.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"Usage pricing schema updated: {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.is_file() or SNAPSHOT.read_text(encoding="utf-8") != rendered:
        print("Usage pricing schema mismatch; run scripts/check_usage_pricing_schema.py --write")
        return 1
    print("Usage pricing schema check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
