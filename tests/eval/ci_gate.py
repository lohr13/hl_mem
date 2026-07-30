"""在无 API key 环境运行 recall_v2 契约与确定性 fixture gate。"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from hl_mem.settings import Settings
from tests.eval.eval_runner import _load_rows, _sha256_utf8_lf, run
from tests.eval.fixtures.build_ci_snapshot import build_ci_snapshot
from tests.eval.gate_check import GATED_METRICS, check

EVAL_ROOT = Path(__file__).parent
DEFAULT_DATASET = EVAL_ROOT / "datasets" / "recall_v2.jsonl"
DEFAULT_DATASET_MANIFEST = EVAL_ROOT / "datasets" / "recall_v2.manifest.json"
DEFAULT_RELEASE_CONFIG = EVAL_ROOT / "release_config_v019.json"
DEFAULT_GATE = EVAL_ROOT / "gate_v019.json"
DEFAULT_REPAIR_MANIFEST = EVAL_ROOT / "repair_manifest_v019.json"
DEFAULT_BASELINE = EVAL_ROOT / "baselines" / "baseline_v019_ci.json"
BASELINE_WARNING = "not valid for release decisions; regenerate with real providers in Phase 2"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是 JSON object")
    return value


def _validate_contracts(
    dataset: Path,
    dataset_manifest: dict[str, Any],
    release_config: dict[str, Any],
    gate: dict[str, Any],
    repair_manifest: dict[str, Any],
    baseline: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    rows = _load_rows(dataset)
    slice_counts = dict(sorted(Counter(str(row["slice"]) for row in rows).items()))
    if dataset_manifest.get("dataset_sha256_algorithm") != "sha256-utf8-lf-v1":
        failures.append("dataset manifest 未声明 sha256-utf8-lf-v1")
    if dataset_manifest.get("dataset_sha256") != _sha256_utf8_lf(dataset):
        failures.append("dataset LF 摘要与 manifest 不一致")
    if dataset_manifest.get("case_count") != len(rows):
        failures.append("dataset case_count 与 manifest 不一致")
    if dataset_manifest.get("slice_counts") != slice_counts:
        failures.append("dataset slice_counts 与 manifest 不一致")

    expected_metrics = list(GATED_METRICS)
    compatibility = gate.get("compatibility_gate", {})
    candidate = gate.get("candidate_non_regression", {})
    if compatibility.get("metrics") != expected_metrics:
        failures.append("compatibility gate 主指标与冻结定义不一致")
    if candidate.get("metrics") != expected_metrics:
        failures.append("candidate gate 主指标与冻结定义不一致")
    if float(compatibility.get("max_regression", -1)) != 0.01:
        failures.append("compatibility gate 阈值不是 0.01")
    if float(candidate.get("max_regression", -1)) != 0.01:
        failures.append("candidate gate 阈值不是 0.01")
    slice_gate = gate.get("slice_non_regression", {})
    if float(slice_gate.get("max_regression", -1)) != 0.05:
        failures.append("slice gate 阈值不是 0.05")
    if slice_gate.get("critical_slices") != ["historical", "preference", "no_answer"]:
        failures.append("critical slices 与冻结定义不一致")
    win_condition = gate.get("win_condition", {})
    if float(win_condition.get("min_improvement", -1)) != 0.02:
        failures.append("win condition 阈值不是 0.02")
    if win_condition.get("metrics") != ["recall_at_5 OR mrr"]:
        failures.append("win condition 指标与冻结定义不一致")

    if release_config.get("embedding") != {"provider": "fake", "model": "fake", "dim": 2048}:
        failures.append("release config 的 fake embedding 配置不一致")
    if release_config.get("reranker") != {"mode": "off", "model": "gte-rerank-v2"}:
        failures.append("release config 的 reranker 配置不一致")
    if release_config.get("experiment") != {
        "variable": "index_text_mode",
        "control": "legacy",
        "candidate": "answerable",
    }:
        failures.append("release config 的实验变量定义不一致")
    if release_config.get("artifact_gate", {}).get("require_equal_except") != ["index_text_mode"]:
        failures.append("release config 未限定只有 index_text_mode 可不同")

    repairs = repair_manifest.get("repairs")
    repair_ids = (
        [repair.get("claim_id") for repair in repairs if isinstance(repair, dict)] if isinstance(repairs, list) else []
    )
    if repair_ids != [
        "697dc55a33c84a78a536ca5eb2296ad9",
        "e78d567879c740a28d342d6b872ba9a0",
    ]:
        failures.append("repair manifest 必须且只能包含批准的 2 条 claim")

    if baseline.get("status") != "ci_fixture":
        failures.append("CI baseline status 必须是 ci_fixture")
    if baseline.get("warning") != BASELINE_WARNING:
        failures.append("CI baseline 缺少非发布证据警告")
    required_baseline_metrics = {*GATED_METRICS, "hit_at_5", "low_confidence_rate"}
    if not required_baseline_metrics <= set(baseline.get("metrics", {})):
        failures.append("CI baseline 缺少主指标、hit_at_5 或 low_confidence_rate")
    return failures


def _validate_runtime_config(report: dict[str, Any], release_config: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    config = report.get("config", {})
    settings = config.get("settings", {})
    fixture = report.get("artifacts", {}).get("fixture", {})
    embedding = release_config["embedding"]
    reranker = release_config["reranker"]
    if config.get("embedder") != embedding["provider"]:
        failures.append("报告 embedder provider 与 release config 不一致")
    if fixture.get("embedding_model") != embedding["model"]:
        failures.append("fixture embedding model 与 release config 不一致")
    if int(settings.get("embedding_dim", -1)) != embedding["dim"]:
        failures.append("报告 embedding dim 与 release config 不一致")
    if config.get("reranker") != reranker["mode"]:
        failures.append("报告 reranker mode 与 release config 不一致")
    if fixture.get("index_text_mode") != release_config["experiment"]["control"]:
        failures.append("fixture index_text_mode 不是 legacy control")
    if fixture.get("extractor_provider") != "fake":
        failures.append("fixture 未使用 fake extractor")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 recall_v2 离线 CI fixture gate")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--dataset-manifest", type=Path, default=DEFAULT_DATASET_MANIFEST)
    parser.add_argument("--release-config", type=Path, default=DEFAULT_RELEASE_CONFIG)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--repair-manifest", type=Path, default=DEFAULT_REPAIR_MANIFEST)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()

    dataset_manifest = _load(arguments.dataset_manifest)
    release_config = _load(arguments.release_config)
    gate = _load(arguments.gate)
    repair_manifest = _load(arguments.repair_manifest)
    baseline = _load(arguments.baseline)
    failures = _validate_contracts(
        arguments.dataset,
        dataset_manifest,
        release_config,
        gate,
        repair_manifest,
        baseline,
    )
    with tempfile.TemporaryDirectory(prefix="hl-mem-ci-recall-gate-") as temporary_directory:
        snapshot = Path(temporary_directory) / "recall-v019-ci.db"
        build_ci_snapshot(snapshot, arguments.dataset)
        settings = Settings(
            embedder_mode="fake",
            embedding_dim=2048,
            extractor_mode="fake",
            reranker_mode="off",
            index_text_mode="legacy",
        )
        report = run(snapshot, arguments.dataset, top_k=5, settings=settings)
        if arguments.report:
            arguments.report.parent.mkdir(parents=True, exist_ok=True)
            arguments.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        failures.extend(_validate_runtime_config(report, release_config))
        failures.extend(
            check(
                report,
                baseline,
                tolerance=float(gate["compatibility_gate"]["max_regression"]),
                slice_tolerance=float(gate["slice_non_regression"]["max_regression"]),
                allow_ci_fixture=True,
            )
        )
    if failures:
        print("Recall CI fixture gate: FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    metrics = report["metrics"]
    print(
        "Recall CI fixture gate: PASSED | "
        f"Recall@5={metrics['recall_at_5']:.4f} MRR={metrics['mrr']:.4f} "
        f"P@3={metrics['precision_at_3']:.4f} "
        f"no-answer P/R={metrics['no_answer_precision']:.4f}/{metrics['no_answer_recall']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
