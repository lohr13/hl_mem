from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISOLATED_KEYS = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX")


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ISOLATED_KEYS:
        environment.pop(key, None)
    return environment


def test_runtime_guard_loads_native_dependencies_only_from_expected_venv() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "hl_mem.evaluation.runtime_guard",
            "--expected-venv",
            str(ROOT / ".venv"),
        ],
        cwd=ROOT,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip())
    assert payload["python"] == [sys.version_info.major, sys.version_info.minor]
    assert Path(payload["prefix"]).resolve() == (ROOT / ".venv").resolve()
    assert Path(payload["numpy_file"]).resolve().is_relative_to((ROOT / ".venv").resolve())
    assert payload["numpy_abi"] == f"cp{sys.version_info.major}{sys.version_info.minor}"
    assert payload["sqlite_vec_version"].startswith("v0.1.")


def test_evaluation_package_is_stdlib_first_until_a_consumer_requests_runner() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; import hl_mem.evaluation; "
            "print(json.dumps({'numpy_loaded':'numpy' in sys.modules,'jieba_loaded':'jieba' in sys.modules}))",
        ],
        cwd=ROOT,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "numpy_loaded": False,
        "jieba_loaded": False,
    }
