#!/usr/bin/env python
"""Reject mutable GitHub Action and workflow Docker references."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

ACTION_PATTERN = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"#\s]+)")
IMAGE_PATTERN = re.compile(r"^\s*image:\s*['\"]?([^'\"#\s]+)")
ACTION_SHA_PATTERN = re.compile(r"^.+@[0-9a-fA-F]{40}$")
DOCKER_DIGEST_PATTERN = re.compile(r"^(?:docker://)?.+@sha256:[0-9a-fA-F]{64}$")


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


def check_paths(paths: Iterable[Path]) -> list[str]:
    violations: list[str] = []
    for path in sorted((Path(item) for item in paths), key=lambda item: item.as_posix()):
        display = _display(path)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            action_match = ACTION_PATTERN.match(line)
            if action_match:
                reference = action_match.group(1)
                if reference.startswith("./"):
                    continue
                if reference.startswith("docker://"):
                    if not DOCKER_DIGEST_PATTERN.fullmatch(reference):
                        violations.append(f"{display}:{line_number}: Docker action is not pinned to a sha256 digest")
                    continue
                if not ACTION_SHA_PATTERN.fullmatch(reference):
                    violations.append(f"{display}:{line_number}: remote action is not pinned to a full commit SHA")
                continue
            image_match = IMAGE_PATTERN.match(line)
            if image_match and not DOCKER_DIGEST_PATTERN.fullmatch(image_match.group(1)):
                violations.append(f"{display}:{line_number}: Docker image is not pinned to a sha256 digest")
    return violations


def _default_paths() -> list[Path]:
    workflow_directory = Path(".github/workflows")
    return sorted((*workflow_directory.glob("*.yml"), *workflow_directory.glob("*.yaml")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    arguments = parser.parse_args(argv)
    paths = arguments.paths or _default_paths()
    violations = check_paths(paths)
    if violations:
        print("GitHub Actions pin check failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print(f"GitHub Actions pin check passed: {len(paths)} workflow files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
