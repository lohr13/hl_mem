"""Enforce ratcheting module and callable complexity ceilings using only AST.

The checked-in budget records exceptions for pre-existing hotspots.  Files and
callables without an exception are held to the defaults below.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "hl_mem"
BUDGET_PATH = PROJECT_ROOT / "scripts" / "complexity_budget.json"
BUDGET_REPOSITORY_PATH = "scripts/complexity_budget.json"

DEFAULT_MAX_LINES = 600
DEFAULT_MAX_PARAMS = 10
DEFAULT_MAX_CALLABLE_LINES = 150

EXCLUDED_SOURCE_PREFIXES = ("src/hl_mem/storage/migrations/",)


@dataclass(frozen=True)
class CallableMetric:
    name: str
    params: int
    lines: int


@dataclass(frozen=True)
class ModuleMetric:
    path: str
    lines: int
    callables: tuple[CallableMetric, ...]


@dataclass(frozen=True)
class BudgetEntry:
    path: str
    max_lines: int
    max_params: Mapping[str, int]
    max_callable_lines: Mapping[str, int]


class _CallableVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope: list[str] = []
        self._scope_kinds: list[str] = []
        self.metrics: list[CallableMetric] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self._scope_kinds.append("class")
        self.generic_visit(node)
        self._scope_kinds.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        name = f"<lambda>@{node.lineno}"
        qualified_name = ".".join((*self._scope, name))
        end_line = node.body.end_lineno or node.body.lineno
        self.metrics.append(
            CallableMetric(
                name=qualified_name,
                params=_effective_parameter_count(node.args),
                lines=end_line - node.body.lineno + 1,
            )
        )
        self._scope.append(name)
        self._scope_kinds.append("lambda")
        self.generic_visit(node)
        self._scope_kinds.pop()
        self._scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = ".".join((*self._scope, node.name))
        first_body_line = node.body[0].lineno if node.body else node.lineno
        end_line = node.end_lineno or first_body_line
        is_direct_method = bool(self._scope_kinds and self._scope_kinds[-1] == "class")
        is_static_method = any(_decorator_name(decorator) == "staticmethod" for decorator in node.decorator_list)
        self.metrics.append(
            CallableMetric(
                name=name,
                params=_effective_parameter_count(
                    node.args,
                    exclude_receiver=is_direct_method and not is_static_method,
                ),
                lines=end_line - first_body_line + 1,
            )
        )
        self._scope.append(node.name)
        self._scope_kinds.append("function")
        self.generic_visit(node)
        self._scope_kinds.pop()
        self._scope.pop()


def _decorator_name(decorator: ast.expr) -> str | None:
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    return None


def _effective_parameter_count(arguments: ast.arguments, *, exclude_receiver: bool = False) -> int:
    positional = [*arguments.posonlyargs, *arguments.args]
    if exclude_receiver and positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    count = len(positional) + len(arguments.kwonlyargs)
    return count + int(arguments.vararg is not None) + int(arguments.kwarg is not None)


def _is_excluded(relative_path: str) -> bool:
    return any(relative_path.startswith(prefix) for prefix in EXCLUDED_SOURCE_PREFIXES)


def _iter_source_files() -> Iterable[Path]:
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if not _is_excluded(relative_path):
            yield path


def _measure_modules() -> tuple[ModuleMetric, ...]:
    modules: list[ModuleMetric] = []
    for path in _iter_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        visitor = _CallableVisitor()
        visitor.visit(tree)
        modules.append(
            ModuleMetric(
                path=path.relative_to(PROJECT_ROOT).as_posix(),
                lines=len(source.splitlines()),
                callables=tuple(visitor.metrics),
            )
        )
    return tuple(modules)


def _positive_integer(value: Any, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{description} must be a positive integer")
    return value


def _integer_mapping(value: Any, description: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be an object")
    result: dict[str, int] = {}
    for raw_name, raw_limit in value.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError(f"{description} keys must be non-empty strings")
        result[raw_name] = _positive_integer(raw_limit, f"{description}.{raw_name}")
    return result


def _parse_budget(raw_budget: Any, source: str) -> dict[str, BudgetEntry]:
    if not isinstance(raw_budget, list):
        raise ValueError(f"{source} must contain a JSON array")

    entries: dict[str, BudgetEntry] = {}
    for index, raw_entry in enumerate(raw_budget):
        description = f"{source}[{index}]"
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{description} must be an object")
        path = raw_entry.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"{description}.path must be a non-empty string")
        if path in entries:
            raise ValueError(f"{source} contains duplicate path {path!r}")
        entries[path] = BudgetEntry(
            path=path,
            max_lines=_positive_integer(raw_entry.get("max_lines"), f"{description}.max_lines"),
            max_params=_integer_mapping(raw_entry.get("max_params"), f"{description}.max_params"),
            max_callable_lines=_integer_mapping(
                raw_entry.get("max_callable_lines"), f"{description}.max_callable_lines"
            ),
        )
    return entries


def _load_budget(path: Path = BUDGET_PATH) -> dict[str, BudgetEntry]:
    try:
        raw_budget = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"budget file not found: {path.relative_to(PROJECT_ROOT).as_posix()}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error
    return _parse_budget(raw_budget, path.relative_to(PROJECT_ROOT).as_posix())


def _check_current_budget(modules: Sequence[ModuleMetric], entries: Mapping[str, BudgetEntry]) -> list[str]:
    violations: list[str] = []
    module_by_path = {module.path: module for module in modules}

    for path, entry in entries.items():
        module = module_by_path.get(path)
        if module is None:
            violations.append(f"{path}: allowlist path does not exist or is excluded")
            continue
        callable_names = {metric.name for metric in module.callables}
        for name in sorted(set(entry.max_params) | set(entry.max_callable_lines)):
            if name not in callable_names:
                violations.append(f"{path}::{name}: allowlist callable does not exist")

    for module in modules:
        entry = entries.get(module.path)
        max_lines = entry.max_lines if entry else DEFAULT_MAX_LINES
        if module.lines > max_lines:
            violations.append(f"{module.path}: {module.lines} physical lines > budget {max_lines}")

        for metric in module.callables:
            max_params = entry.max_params.get(metric.name, DEFAULT_MAX_PARAMS) if entry else DEFAULT_MAX_PARAMS
            if metric.params > max_params:
                violations.append(
                    f"{module.path}::{metric.name}: {metric.params} effective parameters > budget {max_params}"
                )
            max_callable_lines = (
                entry.max_callable_lines.get(metric.name, DEFAULT_MAX_CALLABLE_LINES)
                if entry
                else DEFAULT_MAX_CALLABLE_LINES
            )
            if metric.lines > max_callable_lines:
                violations.append(
                    f"{module.path}::{metric.name}: {metric.lines} body lines > budget {max_callable_lines}"
                )

    return violations


def _base_ref(explicit_ref: str | None) -> str:
    if explicit_ref:
        return explicit_ref
    configured_ref = os.environ.get("COMPLEXITY_BUDGET_BASE_REF")
    if configured_ref:
        return configured_ref
    github_base = os.environ.get("GITHUB_BASE_REF")
    if github_base:
        return f"origin/{github_base}"
    return "HEAD"


def _load_git_budget(ref: str) -> dict[str, BudgetEntry] | None:
    resolved_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if resolved_ref.returncode != 0:
        raise ValueError(f"ratchet base ref does not exist: {ref}")

    result = subprocess.run(
        ["git", "show", f"{ref}:{BUDGET_REPOSITORY_PATH}"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    try:
        raw_budget = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {ref}:{BUDGET_REPOSITORY_PATH}: {error}") from error
    return _parse_budget(raw_budget, f"{ref}:{BUDGET_REPOSITORY_PATH}")


def _check_ratchet(current: Mapping[str, BudgetEntry], base: Mapping[str, BudgetEntry]) -> list[str]:
    violations: list[str] = []
    for path in sorted(set(current) | set(base)):
        current_entry = current.get(path)
        base_entry = base.get(path)
        current_lines = current_entry.max_lines if current_entry else DEFAULT_MAX_LINES
        base_lines = base_entry.max_lines if base_entry else DEFAULT_MAX_LINES
        if current_lines > base_lines:
            violations.append(f"{path}: max_lines ceiling raised {base_lines} -> {current_lines}")

        current_params = current_entry.max_params if current_entry else {}
        base_params = base_entry.max_params if base_entry else {}
        for name in sorted(set(current_params) | set(base_params)):
            current_limit = current_params.get(name, DEFAULT_MAX_PARAMS)
            base_limit = base_params.get(name, DEFAULT_MAX_PARAMS)
            if current_limit > base_limit:
                violations.append(f"{path}::{name}: max_params ceiling raised {base_limit} -> {current_limit}")

        current_callable_lines = current_entry.max_callable_lines if current_entry else {}
        base_callable_lines = base_entry.max_callable_lines if base_entry else {}
        for name in sorted(set(current_callable_lines) | set(base_callable_lines)):
            current_limit = current_callable_lines.get(name, DEFAULT_MAX_CALLABLE_LINES)
            base_limit = base_callable_lines.get(name, DEFAULT_MAX_CALLABLE_LINES)
            if current_limit > base_limit:
                violations.append(f"{path}::{name}: max_callable_lines ceiling raised {base_limit} -> {current_limit}")
    return violations


def _headroom(value: int) -> int:
    return min(10, max(5, math.ceil(value * 0.01)))


def _initial_budget(modules: Sequence[ModuleMetric]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for module in modules:
        exceptional_params = {
            metric.name: metric.params + _headroom(metric.params)
            for metric in module.callables
            if metric.params > DEFAULT_MAX_PARAMS
        }
        exceptional_callable_lines = {
            metric.name: metric.lines + _headroom(metric.lines)
            for metric in module.callables
            if metric.lines > DEFAULT_MAX_CALLABLE_LINES
        }
        if module.lines <= DEFAULT_MAX_LINES and not exceptional_params and not exceptional_callable_lines:
            continue
        entry: dict[str, Any] = {
            "path": module.path,
            "max_lines": module.lines + _headroom(module.lines),
        }
        if exceptional_params:
            entry["max_params"] = dict(sorted(exceptional_params.items()))
        if exceptional_callable_lines:
            entry["max_callable_lines"] = dict(sorted(exceptional_callable_lines.items()))
        entries.append(entry)
    return entries


def _print_violations(title: str, violations: Sequence[str]) -> None:
    print(title, file=sys.stderr)
    for violation in violations:
        print(f"  - {violation}", file=sys.stderr)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="print an initial allowlist JSON document")
    parser.add_argument("--ratchet", action="store_true", help="reject ceiling increases relative to the git base")
    parser.add_argument("--base-ref", help="git ref used as the ratchet base (defaults to the PR base or HEAD)")
    arguments = parser.parse_args()
    if arguments.init and arguments.ratchet:
        parser.error("--init and --ratchet cannot be used together")
    if arguments.base_ref and not arguments.ratchet:
        parser.error("--base-ref requires --ratchet")
    return arguments


def main() -> int:
    arguments = _parse_arguments()
    try:
        modules = _measure_modules()
        if arguments.init:
            print(json.dumps(_initial_budget(modules), ensure_ascii=False, indent=2))
            return 0

        budget = _load_budget()
        violations = _check_current_budget(modules, budget)
        if violations:
            _print_violations("Complexity budget violations:", violations)
            return 1

        if arguments.ratchet:
            ref = _base_ref(arguments.base_ref)
            base_budget = _load_git_budget(ref)
            if base_budget is None:
                print(
                    f"Complexity ratchet: {ref}:{BUDGET_REPOSITORY_PATH} does not exist; "
                    "treating this as the initial budget rollout."
                )
            else:
                ratchet_violations = _check_ratchet(budget, base_budget)
                if ratchet_violations:
                    _print_violations("Complexity ratchet violations:", ratchet_violations)
                    return 1

        print(f"Complexity budget OK: checked {len(modules)} modules.")
        return 0
    except (OSError, SyntaxError, ValueError) as error:
        print(f"Complexity budget check failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
