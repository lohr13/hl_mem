#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$root_dir/hl_mem.toml" ]]; then
    echo "Missing configuration file: $root_dir/hl_mem.toml" >&2
    exit 1
fi

if [[ -x "$root_dir/.venv/bin/python" ]]; then
    python_exe="$root_dir/.venv/bin/python"
elif [[ -x "$root_dir/.venv/Scripts/python.exe" ]]; then
    python_exe="$root_dir/.venv/Scripts/python.exe"
else
    echo "Missing virtual environment Python under $root_dir/.venv" >&2
    exit 1
fi

cd "$root_dir"
exec "$python_exe" "$root_dir/start_server.py"
