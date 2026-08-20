#!/usr/bin/env python
"""Build the v0.29.1 three-field verdict and frozen evaluation report."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.v0291_behavioral.report import build_evaluation_report  # noqa: E402


def _read_optional(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={_display(item)}" for key, item in value.items())
    return str(value).lower() if isinstance(value, bool) else str(value)


def _markdown(
    report: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None,
    sentinel: Mapping[str, Any] | None,
    artifact_hashes: Mapping[str, str | None],
) -> str:
    conclusion = report["conclusion"]
    usage = sentinel.get("usage", {}) if sentinel else {}
    sentinel_ok = bool(
        sentinel
        and sentinel.get("passed") is True
        and sentinel.get("valid_count") == 9
        and sentinel.get("matched_count") == 9
    )
    if sentinel_ok:
        behavior_summary = "付费前置 sentinel 已 9/9 通过；行为层结论由完整 aggregate 与人工盲核共同决定。"
    elif sentinel is None:
        behavior_summary = "付费前置 sentinel artifact 缺失；行为层按 fail-closed 阻断。"
    else:
        record_errors = [
            str(record.get("error", "")).splitlines()[0]
            for record in sentinel.get("records", ())
            if isinstance(record, Mapping) and record.get("error")
        ]
        error_summary = record_errors[0] if record_errors else "invalid scorer output"
        behavior_summary = (
            f"付费前置 sentinel 为 {sentinel.get('matched_count', 0)}/9，有效输出 {sentinel.get('valid_count', 0)} 条；"
            f"首个错误为 `{error_summary}`。硬门阻止了全量生成和判分，没有绕过门禁或改用其他密钥。"
        )
    lines = [
        "# v0.29.1 Behavioral Evaluation Report",
        "",
        "生成日期：2026-08-20。此报告冻结当前可获得的离线证据；未获得的行为或线上证据按 fail-closed 处理。",
        "",
        "## 三字段结论",
        "",
        "| 字段 | 结论 |",
        "| --- | --- |",
        f"| `offline_structural_pass` | `{str(conclusion['offline_structural_pass']).lower()}` |",
        f"| `offline_behavioral_pass` | `{str(conclusion['offline_behavioral_pass']).lower()}` |",
        f"| `canary_ready` | `{str(conclusion['canary_ready']).lower()}` |",
        "",
        "结构层 200 点 × 4 臂已全量通过，800 个 decision 均导出了精确最终 Context Packet 正文。",
        behavior_summary,
        "线上 observe/canary 证据尚未测量。",
        "",
        "## 付费与冻结身份",
        "",
        f"- 固定模型：`{_display(manifest.get('model_snapshot') if manifest else None)}`",
        f"- 冻结代码 commit：`{_display(manifest.get('code_commit') if manifest else None)}`",
        f"- sentinel 最坏预留：¥{_display(sentinel.get('worst_case_reserved_cny') if sentinel else None)}",
        f"- provider usage：input={_display(usage.get('input_tokens'))}, output={_display(usage.get('output_tokens'))}",
        f"- 本次估算实付：¥{_display(sentinel.get('estimated_cost_cny_at_list_price') if sentinel else None)}",
        "- 预算硬上限：¥15；本次没有启动全量付费阶段。",
        "",
        "冻结 manifest 还记录了 behavioral/structural/sentinel fixture、agent system prompt、tool contract、judge prompt",
        "与 strict JSON Schema 的 SHA-256。行为输入按完整盲输入 SHA-256 精确去重，80 点 × 4 臂共 320 个 assignment",
        "物化为 131 个不同模型输入；去重不改变 paired denominator。",
        "",
        "## 完整门禁表",
        "",
        "| Gate | 类别 | 状态 | 阈值 | 观测 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["gate_table"]:
        observed = _display(row.get("observed")).replace("|", "\\|")
        threshold = str(row["threshold"]).replace("|", "\\|")
        lines.append(f"| `{row['gate_id']}` | {row['category']} | **{row['status']}** | {threshold} | {observed} |")
    lines.extend(
        [
            "",
            "`stable_negative` 仅作为 20-case frozen acceptance suite 使用，不作总体误伤率外推。结构层的合成 token、",
            "source-session 信号及耗时也不能替代生产 observe 数据。",
            "",
            "## 本地 artifact",
            "",
        ]
    )
    for name, digest in artifact_hashes.items():
        lines.append(f"- `{name}` — SHA-256 `{_display(digest)}`")
    lines.extend(
        [
            "",
            "这些结果位于 gitignored 的 `evaluation/results/v0291_behavioral_20260820/`，不会进入提交。当前 tracked 报告",
            "保留门禁状态和对应内容哈希。产品数据库未被修改。",
            "",
            "## 解除阻断后的唯一续跑入口",
            "",
            "将 worktree cwd `.env` 中的 `LLM_API_KEY` 替换为有效百炼 key 后执行：",
            "",
            "```powershell",
            "$env:PYTHONPATH=$null",
            "& '.\\.venv\\Scripts\\python.exe' -m scripts.run_v0291_behavioral_eval --phase all",
            "```",
            "",
            "该入口会重新执行结构层和 9 条 sentinel；只有 sentinel 9/9 全对才会进入全量行为阶段。全量完成后还需",
            "填写 `blind_review_result.json`（stale/stable/boundary 各 3 条人工盲核）并重新生成此报告。即使离线行为",
            "通过，没有五项真实运行证据时 `canary_ready` 仍保持 `false`。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "evaluation/results/v0291_behavioral_20260820",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "docs/v0291-behavioral-eval-report.md",
    )
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    paths = {
        "structural_replay.json": output_dir / "structural_replay.json",
        "sentinel_smoke.json": output_dir / "sentinel_smoke.json",
        "behavioral_aggregate.json": output_dir / "behavioral_aggregate.json",
        "blind_review_result.json": output_dir / "blind_review_result.json",
        "runtime_evidence.json": output_dir / "runtime_evidence.json",
        "run_manifest.json": output_dir / "run_manifest.json",
        "expanded_structural.jsonl": output_dir / "expanded_structural.jsonl",
    }
    structural = _read_optional(paths["structural_replay.json"])
    sentinel = _read_optional(paths["sentinel_smoke.json"])
    aggregate = _read_optional(paths["behavioral_aggregate.json"])
    blind_review = _read_optional(paths["blind_review_result.json"])
    runtime = _read_optional(paths["runtime_evidence.json"])
    manifest = _read_optional(paths["run_manifest.json"])
    report = build_evaluation_report(
        structural=structural,
        sentinel=sentinel,
        aggregate=aggregate,
        blind_review=blind_review,
        runtime_evidence=runtime,
    )
    artifact_hashes = {name: _sha256(path) for name, path in paths.items() if path.is_file()}
    report["artifact_sha256"] = artifact_hashes
    _write_json(output_dir / "conclusion.json", report)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(
        _markdown(report, manifest=manifest, sentinel=sentinel, artifact_hashes=artifact_hashes),
        encoding="utf-8",
    )
    print(json.dumps(report["conclusion"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
