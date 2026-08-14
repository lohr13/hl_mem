"""召回评测发布门禁。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

GATED_METRICS = ("recall_at_5", "mrr", "precision_at_3", "no_answer_precision", "no_answer_recall")


def _load(path: Path) -> dict[str, Any]:
    """读取 JSON 对象。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return value


def check(
    report: dict[str, Any],
    baseline: dict[str, Any],
    tolerance: float,
    slice_tolerance: float,
    *,
    allow_ci_fixture: bool = False,
) -> list[str]:
    """返回全部门禁失败原因。"""
    failures: list[str] = []
    status = baseline.get("status")
    if status == "ci_fixture" and not allow_ci_fixture:
        return ["ci_fixture baseline 不能用于正式发布决策"]
    if status not in ({"ready", "ci_fixture"} if allow_ci_fixture else {"ready"}):
        return ["baseline 尚未用对应的冻结 snapshot 初始化"]
    if report.get("schema_version") != baseline.get("schema_version"):
        failures.append("report schema_version 与 baseline 不一致")
    if report.get("artifacts", {}).get("dataset_sha256") != baseline.get("dataset_sha256"):
        failures.append("数据集哈希与 baseline 不一致")
    if status == "ci_fixture":
        current_fixture = report.get("artifacts", {}).get("fixture", {})
        if current_fixture.get("fixture_sha256") != baseline.get("fixture_sha256"):
            failures.append("CI fixture 摘要与 baseline 不一致")
    elif report.get("artifacts", {}).get("snapshot_sha256") != baseline.get("snapshot_sha256"):
        failures.append("snapshot 哈希与 baseline 不一致")
    if report.get("case_count") != baseline.get("case_count"):
        failures.append("case_count 与 baseline 不一致")
    if report.get("slice_counts") != baseline.get("slice_counts"):
        failures.append("slice 分布与 baseline 不一致")
    if int(report.get("total_forbidden_hits", 0)) > 0:
        failures.append("存在 forbidden hits")
    if float(report.get("http_success_rate", 0.0)) < 1.0:
        failures.append("http_success_rate 低于 1.0")
    for metric in GATED_METRICS:
        if metric not in report.get("metrics", {}) or metric not in baseline.get("metrics", {}):
            failures.append(f"缺少门禁指标: {metric}")
            continue
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
    parser.add_argument(
        "--allow-ci-fixture",
        action="store_true",
        help="仅用于离线 CI；显式允许 status=ci_fixture 的非发布 baseline",
    )
    arguments = parser.parse_args()
    if arguments.tolerance < 0 or arguments.slice_tolerance < 0:
        parser.error("容差不得为负数")
    failures = check(
        _load(arguments.report),
        _load(arguments.baseline),
        arguments.tolerance,
        arguments.slice_tolerance,
        allow_ci_fixture=arguments.allow_ci_fixture,
    )
    if failures:
        print("Recall gate: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Recall gate: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
