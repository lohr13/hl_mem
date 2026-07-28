#!/usr/bin/env python
"""校验 MCP 工具契约快照。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hl_mem.mcp.server import get_tool_schemas

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "docs/mcp-tools.json"


def rendered_schema() -> str:
    """返回确定性序列化的 MCP 工具 JSON。"""
    return (
        json.dumps(get_tool_schemas(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def main() -> int:
    """更新或校验 MCP 工具快照。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()
    if args.update:
        SNAPSHOT.write_text(rendered, encoding="utf-8")
        print(f"MCP snapshot updated: {SNAPSHOT.relative_to(ROOT)}")
        return 0
    if not SNAPSHOT.exists() or SNAPSHOT.read_text(encoding="utf-8") != rendered:
        print("MCP snapshot mismatch; run scripts/check_mcp_snapshot.py --update")
        return 1
    print("MCP snapshot check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
