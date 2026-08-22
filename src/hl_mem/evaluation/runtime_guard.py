"""Stdlib-first runtime guard for evaluation processes."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ISOLATED_ENVIRONMENT_KEYS = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX")


@dataclass(frozen=True)
class RuntimeDiagnostics:
    python: tuple[int, int]
    prefix: str
    numpy_version: str
    numpy_file: str
    numpy_abi: str
    sqlite_vec_version: str
    sqlite_vec_file: str


class RuntimeEnvironmentError(RuntimeError):
    """The evaluation runtime is not isolated or ABI-compatible."""


def clean_python_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment without host Python injection variables."""

    cleaned = dict(environment)
    for key in ISOLATED_ENVIRONMENT_KEYS:
        cleaned.pop(key, None)
    return cleaned


def relaunch_evaluation_script(script: Path, argv: Sequence[str], repo_root: Path) -> int | None:
    """Relaunch a direct script with the project interpreter before heavy imports."""

    expected_venv = (repo_root / ".venv").resolve()
    contaminated = any(os.environ.get(key) for key in ISOLATED_ENVIRONMENT_KEYS)
    if Path(sys.prefix).resolve() == expected_venv and not contaminated:
        check_runtime(expected_venv)
        return None
    python = expected_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise RuntimeEnvironmentError(f"expected evaluation interpreter does not exist: {python}")
    completed = subprocess.run(
        [str(python), str(script.resolve()), *argv],
        cwd=repo_root.resolve(),
        env=clean_python_environment(os.environ),
        check=False,
    )
    return completed.returncode


def expected_python_from_venv(venv: Path) -> tuple[int, int]:
    """Read the expected CPython major/minor from the venv metadata."""

    configuration = venv.resolve() / "pyvenv.cfg"
    try:
        lines = configuration.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeEnvironmentError(f"cannot read expected venv metadata: {configuration}") from error
    values = {
        key.strip().casefold(): value.strip()
        for line in lines
        if "=" in line
        for key, value in [line.split("=", maxsplit=1)]
    }
    version = values.get("version_info") or values.get("version")
    if not version:
        raise RuntimeEnvironmentError(f"venv metadata has no version_info: {configuration}")
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except (TypeError, ValueError) as error:
        raise RuntimeEnvironmentError(f"invalid venv version_info {version!r}: {configuration}") from error
    return major, minor


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _module_file(module: Any, name: str) -> Path:
    value = getattr(module, "__file__", None)
    if not value:
        raise RuntimeEnvironmentError(f"{name} has no concrete module file")
    return Path(str(value)).resolve()


def check_runtime(expected_venv: Path) -> RuntimeDiagnostics:
    """Validate interpreter, NumPy origin/ABI, and sqlite-vec loading."""

    inherited = [key for key in ISOLATED_ENVIRONMENT_KEYS if os.environ.get(key)]
    if inherited:
        raise RuntimeEnvironmentError(f"inherited Python environment variables: {', '.join(inherited)}")
    expected_venv = expected_venv.resolve()
    expected_python = expected_python_from_venv(expected_venv)
    actual_python = (sys.version_info.major, sys.version_info.minor)
    if actual_python != expected_python:
        raise RuntimeEnvironmentError(f"Python {actual_python} does not match venv metadata {expected_python}")
    actual_prefix = Path(sys.prefix).resolve()
    if actual_prefix != expected_venv:
        raise RuntimeEnvironmentError(f"sys.prefix {actual_prefix} is not expected venv {expected_venv}")

    numpy = importlib.import_module("numpy")
    numpy_file = _module_file(numpy, "numpy")
    if not _inside(numpy_file, expected_venv):
        raise RuntimeEnvironmentError(f"numpy loaded outside expected venv: {numpy_file}")
    multiarray = importlib.import_module("numpy._core._multiarray_umath")
    multiarray_file = _module_file(multiarray, "numpy._core._multiarray_umath")
    cache_tag = str(sys.implementation.cache_tag or "")
    compact_abi = f"cp{actual_python[0]}{actual_python[1]}"
    if compact_abi not in multiarray_file.name and cache_tag not in multiarray_file.name:
        raise RuntimeEnvironmentError(
            f"numpy extension {multiarray_file.name!r} does not match interpreter ABI {cache_tag!r}"
        )

    sqlite_vec = importlib.import_module("sqlite_vec")
    sqlite_vec_file = _module_file(sqlite_vec, "sqlite_vec")
    if not _inside(sqlite_vec_file, expected_venv):
        raise RuntimeEnvironmentError(f"sqlite_vec loaded outside expected venv: {sqlite_vec_file}")
    load_extension = getattr(sqlite_vec, "load", None)
    if not callable(load_extension):
        raise RuntimeEnvironmentError("sqlite_vec.load is unavailable")
    connection = sqlite3.connect(":memory:")
    try:
        connection.enable_load_extension(True)
        try:
            load_extension(connection)
        finally:
            connection.enable_load_extension(False)
        sqlite_vec_version = str(connection.execute("SELECT vec_version()").fetchone()[0])
    except Exception as error:
        raise RuntimeEnvironmentError(f"sqlite_vec native extension cannot load: {error}") from error
    finally:
        connection.close()

    return RuntimeDiagnostics(
        python=actual_python,
        prefix=str(actual_prefix),
        numpy_version=str(numpy.__version__),
        numpy_file=str(numpy_file),
        numpy_abi=compact_abi,
        sqlite_vec_version=sqlite_vec_version,
        sqlite_vec_file=str(sqlite_vec_file),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-venv", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        diagnostics = check_runtime(arguments.expected_venv)
    except Exception as error:
        print(
            json.dumps(
                {"ok": False, "error_type": type(error).__name__, "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, **asdict(diagnostics)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
