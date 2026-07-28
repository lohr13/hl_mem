#!/usr/bin/env python
"""校验 FastAPI OpenAPI 契约快照。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ["HL_MEM_ENV"] = "test"

from hl_mem.api.server import app

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "docs/api-schema.json"


def rendered_schema() -> str:
    """返回确定性序列化的 OpenAPI JSON。"""
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    """更新或校验 OpenAPI 快照。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()
    if args.update:
        SNAPSHOT.write_text(rendered, encoding="utf-8")
        print(f"OpenAPI snapshot updated: {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.exists() or SNAPSHOT.read_text(encoding="utf-8") != rendered:
        print("OpenAPI snapshot mismatch; run scripts/check_openapi_snapshot.py --update")
        return 1
    print("OpenAPI snapshot check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
