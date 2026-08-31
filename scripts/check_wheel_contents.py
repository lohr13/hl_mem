"""Validate the boundary between installed evaluation and repository research."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

STABLE_EVALUATION_RUNNER = "hl_mem/evaluation/runner.py"


def check_wheel(path: Path, *, reject_v030: bool = False) -> list[str]:
    """Return release-boundary violations found in *path*."""
    with zipfile.ZipFile(path) as archive:
        members = sorted(name.replace("\\", "/") for name in archive.namelist())
    violations: list[str] = []
    if STABLE_EVALUATION_RUNNER not in members:
        violations.append(f"missing stable evaluation module: {STABLE_EVALUATION_RUNNER}")
    violations.extend(
        f"repository benchmark leaked into wheel: {member}" for member in members if member.startswith("benchmarks/")
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
