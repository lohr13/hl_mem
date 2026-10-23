"""Validate the boundary between installed evaluation and repository research."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

STABLE_EVALUATION_RUNNER = "hl_mem/evaluation/runner.py"
PEP561_MARKER = "hl_mem/py.typed"
_DATABASE_SUFFIXES = (".db", ".db-shm", ".db-wal")
_EXTERNAL_PLUGIN_PREFIXES = ("external_plugins/", "provider_plugins/", "hl_mem_provider_")


def check_wheel(path: Path, *, reject_v030: bool = False) -> list[str]:
    """Return release-boundary violations found in *path*."""
    with zipfile.ZipFile(path) as archive:
        members = sorted(name.replace("\\", "/") for name in archive.namelist())
    violations: list[str] = []
    if STABLE_EVALUATION_RUNNER not in members:
        violations.append(f"missing stable evaluation module: {STABLE_EVALUATION_RUNNER}")
    if PEP561_MARKER not in members:
        violations.append(f"missing PEP 561 marker: {PEP561_MARKER}")
    violations.extend(
        f"repository benchmark leaked into wheel: {member}" for member in members if member.startswith("benchmarks/")
    )
    violations.extend(
        f"Provider live smoke result leaked into wheel: {member}"
        for member in members
        if (
            member.rsplit("/", 1)[-1].casefold().endswith(".json")
            and all(word in member.rsplit("/", 1)[-1].casefold() for word in ("provider", "smoke", "result"))
        )
    )
    violations.extend(
        f"temporary database leaked into wheel: {member}"
        for member in members
        if member.casefold().endswith(_DATABASE_SUFFIXES)
    )
    violations.extend(
        f"external plugin code leaked into wheel: {member}"
        for member in members
        if member.casefold().startswith(_EXTERNAL_PLUGIN_PREFIXES)
    )
    if reject_v030:
        violations.extend(
            f"historical v0.30 evaluation leaked into wheel: {member}"
            for member in members
            if member.startswith("hl_mem/evaluation/v030_")
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--reject-v030", action="store_true")
    args = parser.parse_args()
    violations = check_wheel(args.wheel, reject_v030=args.reject_v030)
    if violations:
        for violation in violations:
            print(violation)
        return 1
    print(f"wheel contents valid: {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
