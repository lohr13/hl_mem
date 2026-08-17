#!/usr/bin/env python
"""校验版本号与 migration 数量在维护文档中保持一致。"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")
URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def get_version() -> str:
    """从包入口读取版本 SSOT。"""
    text = (ROOT / "src/hl_mem/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']v?([^"\']+)["\']', text, re.MULTILINE)
    if match is None:
        raise ValueError("src/hl_mem/__init__.py 中未找到 __version__")
    return match.group(1)


def get_project_version() -> str:
    """从 pyproject.toml 读取发布包版本。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?ms)^\[project\].*?^version\s*=\s*["\']([^"\']+)["\']', text)
    if match is None:
        raise ValueError("pyproject.toml 的 [project] 中未找到 version")
    return match.group(1)


def get_migration_count() -> int:
    """从 SQL migration 文件数量读取 migration SSOT。"""
    return len(list((ROOT / "src/hl_mem/storage/migrations").glob("*.sql")))


def read(path: str) -> str:
    """以 UTF-8 读取项目文档。"""
    return (ROOT / path).read_text(encoding="utf-8")


def tracked_markdown_files(root: Path) -> list[Path]:
    """返回 Git 当前跟踪的 Markdown 文件。"""

    result = subprocess.run(
        ["git", "ls-files", "--", "*.md"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [root / line for line in result.stdout.splitlines() if line]


def find_broken_relative_links(root: Path, markdown_files: Iterable[Path]) -> list[str]:
    """查找 Markdown 中指向不存在文件或目录的相对链接。"""

    root = root.resolve()
    errors: list[str] = []
    for source in markdown_files:
        source = source.resolve()
        source_label = source.relative_to(root).as_posix()
        text = source.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_PATTERN.finditer(text):
            raw_target = match.group(1).strip()
            if raw_target.startswith("<") and ">" in raw_target:
                target = raw_target[1 : raw_target.index(">")]
            else:
                target = raw_target.split(maxsplit=1)[0]
            if not target or target.startswith(("#", "/")) or URI_SCHEME_PATTERN.match(target):
                continue
            path_text = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if path_text and not (source.parent / path_text).exists():
                errors.append(f"{source_label} -> {target}")
    return errors


def check_value(text: str, pattern: str, expected: str | int, label: str) -> list[str]:
    """检查指定模式捕获的值与预期一致。"""
    matches = re.findall(pattern, text, re.MULTILINE | re.IGNORECASE)
    if not matches:
        return [f"  {label}: reference not found (pattern: {pattern})"]
    expected_text = str(expected)
    return [f"  {label}: found '{value}', expected '{expected_text}'" for value in matches if value != expected_text]


def latest_changelog_entry(changelog: str) -> tuple[str, str]:
    """返回 CHANGELOG 最新版本号及其条目正文。"""
    match = re.search(
        r"^##\s+v?(\d+\.\d+\.\d+)\b(?P<body>.*?)(?=^##\s+v?\d+\.\d+\.\d+\b|\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ValueError("docs/CHANGELOG.md 中未找到版本条目")
    return match.group(1), match.group("body")


def main() -> int:
    """运行全部文档一致性检查并返回进程退出码。"""
    try:
        version = get_version()
        project_version = get_project_version()
        migration_count = get_migration_count()
        readme = read("README.md")
        readme_en = read("README_EN.md")
        architecture = read("docs/architecture.md")
        handoff = read("docs/HANDOFF.md")
        capability_matrix = read("docs/capability-matrix.md")
        changelog = read("docs/CHANGELOG.md")
        agents_md = read("AGENTS.md")

        errors: list[str] = []
        if project_version != version:
            errors.append(f"  pyproject.toml version: found '{project_version}', expected '{version}'")
        errors += check_value(
            readme,
            r"shields\.io/badge/version-v?(\d+\.\d+\.\d+)-",
            version,
            "README badge version",
        )
        errors += check_value(
            readme_en,
            r"The current baseline is\s+v?(\d+\.\d+\.\d+)",
            version,
            "README_EN body version",
        )
        errors += check_value(
            readme,
            r"当前基线为\s*v?(\d+\.\d+\.\d+)",
            version,
            "README body version",
        )
        errors += check_value(
            architecture,
            r"Document baseline:\s*v?(\d+\.\d+\.\d+)",
            version,
            "architecture baseline",
        )
        errors += check_value(
            handoff,
            r"\*\*版本\*\*[：:]\s*v?(\d+\.\d+\.\d+)",
            version,
            "HANDOFF version",
        )
        errors += check_value(
            agents_md,
            r"\*\*当前版本[：:]\s*v?(\d+\.\d+\.\d+)",
            version,
            "AGENTS.md version",
        )
        errors += check_value(
            capability_matrix,
            r"基线[：:]\s*v?(\d+\.\d+\.\d+)",
            version,
            "capability matrix baseline",
        )
        errors += check_value(
            readme_en,
            r"The current baseline is.*?\b(\d+)\s+immutable\b.*?\bmigrations\b",
            migration_count,
            "README_EN migrations",
        )
        errors += check_value(
            readme,
            r"当前基线为.*?共\s*(\d+)\s*个.*?Migration",
            migration_count,
            "README migrations",
        )
        errors += check_value(
            architecture,
            r"\b(\d+)\s+immutable SQL migrations\b",
            migration_count,
            "architecture migrations",
        )
        errors += check_value(handoff, r"\b(\d+)\s+migrations\b", migration_count, "HANDOFF migrations")

        errors += [
            f"  Markdown relative link: {broken}"
            for broken in find_broken_relative_links(ROOT, tracked_markdown_files(ROOT))
        ]

        headers = re.findall(r"^##\s+v?(\d+\.\d+\.\d+)\b", changelog, re.MULTILINE)
        duplicates = sorted({header for header in headers if headers.count(header) > 1})
        if duplicates:
            errors.append(f"  CHANGELOG: duplicate version headers: {', '.join(duplicates)}")

        latest_version, _ = latest_changelog_entry(changelog)
        if latest_version != version:
            errors.append(f"  CHANGELOG latest version: found '{latest_version}', expected '{version}'")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Document consistency check failed:\n  {exc}")
        return 1

    if errors:
        print("Document consistency check failed:")
        print("\n".join(errors))
        return 1
    print(f"All docs consistent: v{version}, {migration_count} migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
