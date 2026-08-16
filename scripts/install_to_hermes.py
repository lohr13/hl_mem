#!/usr/bin/env python3
"""将 HL-Mem 适配器安装或升级到 Hermes 插件目录。

Usage:
    python scripts/install_to_hermes.py [--hermes-home PATH] [--dry-run]
"""

from hl_mem.adapters.hermes.deployment import script_main as main

if __name__ == "__main__":
    raise SystemExit(main())
