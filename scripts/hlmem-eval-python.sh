#!/usr/bin/env bash
set -euo pipefail

unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_exe="$repo_root/.venv/bin/python"
expected_venv="$repo_root/.venv"
if [[ ! -x "$python_exe" ]]; then
    python_exe="$repo_root/.venv/Scripts/python.exe"
    if command -v cygpath >/dev/null 2>&1; then
        expected_venv="$(cygpath -w "$expected_venv")"
    fi
fi

if [[ ! -x "$python_exe" ]]; then
    echo "Missing virtual environment Python: $python_exe" >&2
    exit 1
fi

cd "$repo_root"
"$python_exe" -m hl_mem.evaluation.runtime_guard --expected-venv "$expected_venv"
exec "$python_exe" "$@"
