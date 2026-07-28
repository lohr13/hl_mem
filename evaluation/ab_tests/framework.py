"""每次只改变一个变量的 A/B 对比执行器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Variant:
    """单变量试验变体。"""

    name: str
    variable: str
    value: object


def run_ab(
    cases: list[dict[str, Any]],
    variants: list[Variant],
    runner: Callable[[dict[str, Any], Variant], dict[str, float]],
) -> dict[str, dict[str, float]]:
    """执行共享数据集并按变体汇总数值指标均值。"""
    if len({variant.variable for variant in variants}) > 1:
        raise ValueError("A/B run may change only one variable")
    output: dict[str, dict[str, float]] = {}
    for variant in variants:
        rows = [runner(case, variant) for case in cases]
        keys = {key for row in rows for key in row}
        output[variant.name] = {key: sum(row.get(key, 0.0) for row in rows) / max(1, len(rows)) for key in keys}
    return output
