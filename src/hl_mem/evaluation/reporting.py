"""Benchmark JSON 报告与由 JSON 单向渲染的 Markdown 摘要。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def generate_json_report(result: Mapping[str, Any], output: Path) -> Path:
    """把完整评测结果写为稳定排序的 report.json。"""
    output.mkdir(parents=True, exist_ok=True)
    path = output / "report.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _metric_rows(metrics: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for layer, values in metrics.items():
        if not isinstance(values, Mapping):
            rows.append((str(layer), "value", _format_value(values)))
            continue
        for name, value in values.items():
            if isinstance(value, Mapping):
                for nested_name, nested_value in value.items():
                    rows.append((str(layer), f"{name}.{nested_name}", _format_value(nested_value)))
            else:
                rows.append((str(layer), str(name), _format_value(value)))
    return rows


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    if value is None:
        return "not_run"
    return str(value)


def generate_markdown_summary(result: Mapping[str, Any], output: Path) -> Path:
    """只读取 report 数据渲染 summary.md，不重新计算指标。"""
    lines = [
        f"# {result.get('benchmark', 'benchmark')} / {result.get('subset', '')}",
        "",
        f"- Config hash: `{result.get('config_hash', '')}`",
        f"- Dataset hash: `{result.get('run', {}).get('source_hash', '')}`",
        f"- Models: `{json.dumps(result.get('run', {}).get('models', {}), ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Overall metrics",
        "",
        "| Layer | Metric | Value |",
        "| --- | --- | ---: |",
    ]
    lines.extend(
        f"| {layer} | {metric} | {value} |"
        for layer, metric, value in _metric_rows(result.get("metrics", {}))
    )
    lines.extend(["", "## Categories", "", "| Category | Layer | Metric | Value |", "| --- | --- | --- | ---: |"])
    for category, metrics in result.get("categories", {}).items():
        for layer, metric, value in _metric_rows(metrics):
            lines.append(f"| {category} | {layer} | {metric} | {value} |")
    failures = [case for case in result.get("cases", []) if case.get("errors")]
    lines.extend(["", "## Failed cases", ""])
    if failures:
        for case in failures:
            lines.append(f"- `{case.get('case_id')}`: {'; '.join(map(str, case.get('errors', [])))}")
    else:
        lines.append("- None")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
