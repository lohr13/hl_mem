"""召回评测发布门禁。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

GATED_METRICS = ("mrr", "recall_at_5", "no_answer_precision")


def _load(path: Path) -> dict[str, Any]:
    """读取 JSON 对象。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return value


def check(report: dict[str, Any], baseline: dict[str, Any], tolerance: float, slice_tolerance: float) -> list[str]:
    """返回全部门禁失败原因。"""
    failures: list[str] = []
    if baseline.get("status") != "ready":
        return ["baseline 尚未用对应的冻结 snapshot 初始化"]
    if report.get("artifacts", {}).get("dataset_sha256") != baseline.get("dataset_sha256"):
        failures.append("数据集哈希与 baseline 不一致")
    if report.get("artifacts", {}).get("snapshot_sha256") != baseline.get("snapshot_sha256"):
        failures.append("snapshot 哈希与 baseline 不一致")
    for metric in GATED_METRICS:
        current = float(report["metrics"][metric])
        reference = float(baseline["metrics"][metric])
        if current < reference - tolerance:
            failures.append(f"总体 {metric} 退化 {reference - current:.4f}，容差 {tolerance:.4f}")
    for slice_name, reference_slice in baseline.get("slices", {}).items():
        current_slice = report.get("slices", {}).get(slice_name)
        if current_slice is None:
            failures.append(f"缺少 slice: {slice_name}")
            continue
        for metric, reference in reference_slice.get("metrics", {}).items():
            if metric not in current_slice.get("metrics", {}):
                continue
            current = float(current_slice["metrics"][metric])
            if current < float(reference) - slice_tolerance:
                failures.append(
                    f"slice {slice_name} 的 {metric} 退化 {float(reference) - current:.4f}，阈值 {slice_tolerance:.4f}"
                )
    return failures


def main() -> int:
    """命令行入口，0 表示通过，1 表示失败。"""
    parser = argparse.ArgumentParser(description="检查召回评测是否通过发布门禁")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument("--slice-tolerance", type=float, default=0.05)
    arguments = parser.parse_args()
    if arguments.tolerance < 0 or arguments.slice_tolerance < 0:
        parser.error("容差不得为负数")
    failures = check(_load(arguments.report), _load(arguments.baseline), arguments.tolerance, arguments.slice_tolerance)
    if failures:
        print("Recall gate: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Recall gate: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
