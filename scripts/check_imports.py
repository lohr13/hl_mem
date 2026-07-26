"""检查 HL-Mem 分层模块的依赖方向。"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

FORBIDDEN_IMPORTS: dict[str, frozenset[str]] = {
    "core": frozenset({"ingest", "llm", "storage", "api", "workers", "recall", "application"}),
    "domain": frozenset({"storage", "api", "workers", "recall", "ingest", "llm", "application"}),
    "storage": frozenset({"api", "workers", "application"}),
    "application": frozenset({"api"}),
}


@dataclass(frozen=True)
class ImportViolation:
    """描述一处违反分层规则的导入。"""

    path: Path
    line: int
    source_layer: str
    target_layer: str
    module: str

    def render(self, root: Path) -> str:
        """返回适合 CI 输出的错误文本。"""
        try:
            display_path = self.path.relative_to(root)
        except ValueError:
            display_path = self.path
        return (
            f"{display_path}:{self.line}: {self.source_layer}/ must not import "
            f"hl_mem.{self.target_layer} ({self.module})"
        )


def imported_modules(tree: ast.AST, current_module: str) -> Iterable[tuple[int, str]]:
    """枚举 AST 中的导入行号和绝对模块名。"""
    package_parts = current_module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                retained = package_parts[: max(0, len(package_parts) - node.level + 1)]
                base = ".".join((*retained, node.module or ""))
            else:
                base = node.module or ""
            for alias in node.names:
                yield node.lineno, ".".join(part for part in (base, alias.name) if part)


def find_violations(source_root: Path) -> list[ImportViolation]:
    """扫描源码根目录并返回全部依赖方向违规。"""
    package_root = source_root / "hl_mem"
    violations: list[ImportViolation] = []
    for source_layer, forbidden_layers in FORBIDDEN_IMPORTS.items():
        layer_root = package_root / source_layer
        for path in sorted(layer_root.rglob("*.py")):
            module = ".".join(path.relative_to(source_root).with_suffix("").parts)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for line, imported_module in imported_modules(tree, module):
                parts = imported_module.split(".")
                if len(parts) < 2 or parts[0] != "hl_mem" or parts[1] not in forbidden_layers:
                    continue
                violations.append(
                    ImportViolation(
                        path=path,
                        line=line,
                        source_layer=source_layer,
                        target_layer=parts[1],
                        module=imported_module,
                    )
                )
    return violations


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    default_source_root = Path(__file__).resolve().parents[1] / "src"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=default_source_root,
        help="包含 hl_mem package 的源码根目录",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """运行依赖方向检查并返回进程退出码。"""
    args = parse_args(argv)
    source_root = args.source_root.resolve()
    if not (source_root / "hl_mem").is_dir():
        print(f"source root does not contain hl_mem package: {source_root}", file=sys.stderr)
        return 2

    try:
        violations = find_violations(source_root)
    except (OSError, SyntaxError) as exc:
        print(f"failed to inspect imports: {exc}", file=sys.stderr)
        return 2

    if violations:
        for violation in violations:
            print(violation.render(source_root), file=sys.stderr)
        print(f"import boundary check failed: {len(violations)} violation(s)", file=sys.stderr)
        return 1

    print("import boundary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
