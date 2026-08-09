#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$root_dir/hl_mem.toml" ]]; then
    echo "Missing configuration file: $root_dir/hl_mem.toml" >&2
    exit 1
fi

exec "$root_dir/scripts/hlmem-python.sh" "$root_dir/start_server.py"
