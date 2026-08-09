#!/usr/bin/env bash
set -euo pipefail

unset PYTHONPATH PYTHONHOME

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_exe="$repo_root/.venv/Scripts/python.exe"

if [[ ! -x "$python_exe" ]]; then
    echo "Missing virtual environment Python: $python_exe" >&2
    exit 1
fi

cd "$repo_root"
exec "$python_exe" "$@"
