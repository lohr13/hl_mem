#!/usr/bin/env python
"""确保 mypy 不产生超出已提交基线的新错误。"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mypy_baseline.txt"
ERROR_PATTERN = re.compile(r"^(.*?):\d+(?::\d+)?: error: (.*?)  \[([^\]]+)\]$")


def normalized_errors(output: str) -> Counter[str]:
    """去除易变行号，同时保留文件、错误文本、错误码与重复次数。"""
    errors: Counter[str] = Counter()
    for raw_line in output.splitlines():
        match = ERROR_PATTERN.match(raw_line.strip())
        if match:
            path, message, code = match.groups()
            message = re.sub(r"\bline \d+\b", "line <LINE>", message)
            errors[f"{Path(path).as_posix()} | {code} | {message}"] += 1
    return errors


def serialize(errors: Counter[str]) -> str:
    """将错误多重集序列化为稳定文本。"""
    return "\n".join(f"{count}\t{error}" for error, count in sorted(errors.items())) + "\n"


def read_baseline() -> Counter[str]:
    """读取已提交的错误基线。"""
    errors: Counter[str] = Counter()
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        count, error = line.split("\t", 1)
        errors[error] = int(count)
    return errors


def main() -> int:
    """运行 mypy，并在发现新错误时失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="以当前 mypy 输出更新基线")
    args = parser.parse_args()
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "src/hl_mem/", "--ignore-missing-imports"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    current = normalized_errors(output)
    if args.update:
        BASELINE.write_text(serialize(current), encoding="utf-8")
        print(f"mypy baseline updated: {sum(current.values())} errors")
        return 0
    baseline = read_baseline()
    new_errors = current - baseline
    if new_errors:
        print("mypy baseline check failed; new errors:")
        print(serialize(new_errors), end="")
        return 1
    print(f"mypy baseline check passed: {sum(current.values())} current / {sum(baseline.values())} baseline errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
