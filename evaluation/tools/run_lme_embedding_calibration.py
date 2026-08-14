"""Calibrate Q0/Q1/Q2 semantic-dedup thresholds on frozen pair data.

The tool is evaluation-only.  It selects each configuration's threshold on
the dev split under a zero-observed-false-merge constraint, freezes that
threshold, and evaluates it once on validation.  Existing embedding cache
batches are required unless ``--allow-api`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation" / "tools"))

from run_embedding_ablation import (  # noqa: E402
    CONFIGS,
    DashScopeEmbeddingClient,
    _load_env_value,
    embed_remote,
)

CONFIG_CODES = ("Q0", "Q1", "Q2")
FIXED_OBSERVATION_THRESHOLDS = (0.82, 0.92, 0.95)
DEFAULT_CLAIM_PAIRS = ROOT / "evaluation" / "datasets" / "claim_pair_eval_v1.jsonl"
DEFAULT_LME_PAIRS = ROOT / "evaluation" / "results" / "lme_embedding_sel_v1_pair_gold.jsonl"
DEFAULT_CACHE = ROOT / "evaluation" / "cache" / "embedding_ablation_v1"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "lme_embedding_calibration_v1.json"
DEFAULT_REPORT = ROOT / "evaluation" / "results" / "lme_embedding_calibration_v1_report.md"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL with optional UTF-8 BOM, blank lines, and comment headers."""

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row at {path}:{line_number} must be an object")
        rows.append(value)
    return rows


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def split_lme_ids(pair_ids: Sequence[str]) -> dict[str, str]:
    """Split LME pairs 50/50 by a stable hash ordering."""

    if len(set(pair_ids)) != len(pair_ids):
        raise ValueError("LME pair IDs must be unique")
    ordered = sorted(
        pair_ids,
        key=lambda pair_id: (hashlib.sha256(f"lme:{pair_id}".encode("utf-8")).hexdigest(), pair_id),
    )
    dev_count = len(ordered) // 2
    return {pair_id: "dev" if index < dev_count else "validation" for index, pair_id in enumerate(ordered)}


def candidate_thresholds(scores: Sequence[float]) -> list[float]:
    """Return all unique-score midpoints plus a boundary above the maximum."""

    unique = sorted(set(float(score) for score in scores))
    if not unique:
        raise ValueError("threshold calibration needs at least one score")
    candidates = [round(left + (right - left) / 2.0, 15) for left, right in zip(unique[:-1], unique[1:], strict=True)]
    candidates.append(math.nextafter(unique[-1], math.inf))
    return candidates


def _binomial_cdf(successes: int, total: int, probability: float) -> float:
    return sum(
        math.comb(total, count) * probability**count * (1.0 - probability) ** (total - count)
        for count in range(successes + 1)
    )


def clopper_pearson_upper(successes: int, total: int, confidence: float = 0.95) -> float | None:
    """Exact one-sided binomial confidence upper bound."""

    if total < 0 or successes < 0 or successes > total:
        raise ValueError("binomial counts must satisfy 0 <= successes <= total")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    if total == 0:
        return None
    if successes == total:
        return 1.0
    alpha = 1.0 - confidence
    low, high = 0.0, 1.0
    for _ in range(100):
        midpoint = (low + high) / 2.0
        if _binomial_cdf(successes, total, midpoint) > alpha:
            low = midpoint
        else:
            high = midpoint
    return high


def metrics_at_threshold(
    labels: Sequence[bool],
    scores: Sequence[float],
    threshold: float,
) -> dict[str, Any]:
    if not labels or len(labels) != len(scores):
        raise ValueError("metrics need aligned non-empty labels and scores")
    predictions = [float(score) >= threshold for score in scores]
    tp = sum(prediction and label for prediction, label in zip(predictions, labels, strict=True))
    fp = sum(prediction and not label for prediction, label in zip(predictions, labels, strict=True))
    fn = sum(not prediction and label for prediction, label in zip(predictions, labels, strict=True))
    tn = sum(not prediction and not label for prediction, label in zip(predictions, labels, strict=True))
    positives = tp + fn
    negatives = fp + tn
    return {
        "threshold": float(threshold),
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "equivalent_total": positives,
        "negative_total": negatives,
        "equivalent_recall": tp / positives if positives else None,
        "false_merge_rate": fp / negatives if negatives else None,
        "false_merge_upper_95_one_sided": clopper_pearson_upper(fp, negatives),
    }


def select_safe_threshold(labels: Sequence[bool], scores: Sequence[float]) -> dict[str, Any]:
    """Maximize equivalent TP under dev FP=0; break ties toward higher thresholds."""

    candidates = candidate_thresholds(scores)
    evaluated = [metrics_at_threshold(labels, scores, threshold) for threshold in candidates]
    safe = [metrics for metrics in evaluated if metrics["fp"] == 0]
    if not safe:
        raise RuntimeError("no zero-false-merge threshold candidate exists")
    selected = max(safe, key=lambda metrics: (metrics["tp"], metrics["threshold"]))
    return {
        "threshold": selected["threshold"],
        "selection_rule": "dev_fp_eq_0_then_max_equivalent_tp_then_max_threshold",
        "candidate_source": "all_unique_score_midpoints_plus_upper_boundary",
        "candidate_count": len(candidates),
        "metrics": selected,
    }


def pairwise_comparison(
    pair_ids: Sequence[str],
    labels: Sequence[bool],
    predictions_a: Sequence[bool],
    predictions_b: Sequence[bool],
) -> dict[str, Any]:
    if not (len(pair_ids) == len(labels) == len(predictions_a) == len(predictions_b)):
        raise ValueError("pairwise comparison inputs must have equal lengths")

    all_win_ids: list[str] = []
    all_loss_ids: list[str] = []
    all_tie_ids: list[str] = []
    equivalent_win_ids: list[str] = []
    equivalent_loss_ids: list[str] = []
    equivalent_tie_ids: list[str] = []
    for pair_id, label, prediction_a, prediction_b in zip(
        pair_ids,
        labels,
        predictions_a,
        predictions_b,
        strict=True,
    ):
        correct_a = prediction_a == label
        correct_b = prediction_b == label
        if correct_a and not correct_b:
            all_win_ids.append(pair_id)
        elif correct_b and not correct_a:
            all_loss_ids.append(pair_id)
        else:
            all_tie_ids.append(pair_id)

        if label:
            if prediction_a and not prediction_b:
                equivalent_win_ids.append(pair_id)
            elif prediction_b and not prediction_a:
                equivalent_loss_ids.append(pair_id)
            else:
                equivalent_tie_ids.append(pair_id)

    def summarize(wins: list[str], losses: list[str], ties: list[str]) -> dict[str, Any]:
        return {
            "wins": len(wins),
            "losses": len(losses),
            "ties": len(ties),
            "net_wins": len(wins) - len(losses),
            "win_pair_ids": wins,
            "loss_pair_ids": losses,
            "tie_pair_ids": ties,
        }

    return {
        "all_pairs": summarize(all_win_ids, all_loss_ids, all_tie_ids),
        "equivalent_pairs": summarize(equivalent_win_ids, equivalent_loss_ids, equivalent_tie_ids),
    }


def _pair_text(side: Mapping[str, Any]) -> str:
    return " ".join(
        str(part)
        for part in (
            side.get("subject", ""),
            side.get("predicate", ""),
            side.get("value", ""),
            side.get("canonical_slot", ""),
        )
        if part
    )


class _CacheOnlyClient:
    def request(self, config: Any, role: str, texts: Sequence[str]) -> None:
        raise RuntimeError(
            f"embedding cache miss for {config.code}/{role} ({len(texts)} texts); "
            "rerun with --allow-api only if API replenishment is intended"
        )


def _old_pair_scores(
    rows: Sequence[Mapping[str, Any]],
    cache_dir: Path,
    *,
    allow_api: bool,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    texts = sorted({_pair_text(side) for row in rows for side in (row["left"], row["right"])})
    if not texts or any(not text for text in texts):
        raise ValueError("claim pair text construction produced an empty corpus")
    if allow_api:
        api_key = _load_env_value(ROOT / ".env", "EMBEDDING_API_KEY")
        client: Any = DashScopeEmbeddingClient(api_key=api_key)
    else:
        client = _CacheOnlyClient()

    scores: dict[str, dict[str, float]] = {str(row["pair_id"]): {} for row in rows}
    costs: dict[str, Any] = {}
    for code in CONFIG_CODES:
        output = embed_remote(
            client,
            CONFIGS[code],
            "document",
            texts,
            cache_dir=cache_dir,
            use_cache=True,
        )
        norms = np.linalg.norm(output.dense, axis=1, keepdims=True)
        if np.any(norms == 0.0):
            raise ValueError(f"{code} cache contains a zero-norm vector")
        normalized = output.dense / norms
        index = {text: position for position, text in enumerate(texts)}
        for row in rows:
            left = normalized[index[_pair_text(row["left"])]]
            right = normalized[index[_pair_text(row["right"])]]
            scores[str(row["pair_id"])][code] = float(left @ right)
        costs[code] = {
            "cache_hit_batches": output.cost.cache_hit_batches,
            "network_api_calls_this_run": output.cost.network_api_calls_this_run,
            "tokens_this_run": output.cost.tokens if output.cost.network_api_calls_this_run else 0,
        }
    return scores, {"unique_texts": len(texts), "configs": costs}


def _validate_inputs(
    claim_rows: Sequence[Mapping[str, Any]],
    lme_rows: Sequence[Mapping[str, Any]],
) -> None:
    if len(claim_rows) != 80:
        raise ValueError(f"claim_pair_eval_v1 must contain 80 pairs, got {len(claim_rows)}")
    if len(lme_rows) != 100:
        raise ValueError(f"LME pair gold must contain 100 pairs, got {len(lme_rows)}")
    claim_ids = [str(row.get("pair_id", "")) for row in claim_rows]
    lme_ids = [str(row.get("pair_id", "")) for row in lme_rows]
    if "" in claim_ids or len(set(claim_ids)) != len(claim_ids):
        raise ValueError("claim_pair_eval_v1 pair IDs must be present and unique")
    if "" in lme_ids or len(set(lme_ids)) != len(lme_ids):
        raise ValueError("LME pair IDs must be present and unique")
    if any(row.get("split") not in {"dev", "test"} for row in claim_rows):
        raise ValueError("claim_pair_eval_v1 splits must be dev/test")
    if any(row.get("label") == "" or row.get("label") is None for row in claim_rows):
        raise ValueError("claim_pair_eval_v1 labels must be present")
    for row in lme_rows:
        if row.get("label") != "non_equivalent":
            raise ValueError("all frozen LME calibration pairs must be non_equivalent")
        if any(code not in (row.get("scores") or {}) for code in CONFIG_CODES):
            raise ValueError(f"LME pair {row.get('pair_id')} lacks a Q0/Q1/Q2 score")


def _freeze_pairs(
    claim_rows: Sequence[Mapping[str, Any]],
    lme_rows: Sequence[Mapping[str, Any]],
    old_scores: Mapping[str, Mapping[str, float]],
) -> list[dict[str, Any]]:
    frozen: list[dict[str, Any]] = []
    for row in claim_rows:
        source_id = str(row["pair_id"])
        source_label = str(row["label"])
        frozen.append(
            {
                "pair_id": f"claim_pair_eval_v1:{source_id}",
                "source": "claim_pair_eval_v1",
                "source_pair_id": source_id,
                "language": "Chinese",
                "split": "dev" if row["split"] == "dev" else "validation",
                "label": "equivalent" if source_label == "equivalent" else "negative",
                "source_label": source_label,
                "provisional": False,
                "left": row["left"],
                "right": row["right"],
                "embedding_text": {
                    "left": _pair_text(row["left"]),
                    "right": _pair_text(row["right"]),
                    "semantics": "subject + predicate + value + canonical_slot",
                },
                "scores": {code: float(old_scores[source_id][code]) for code in CONFIG_CODES},
            }
        )

    lme_split = split_lme_ids([str(row["pair_id"]) for row in lme_rows])
    for row in lme_rows:
        source_id = str(row["pair_id"])
        frozen.append(
            {
                "pair_id": f"lme_embedding_sel_v1:{source_id}",
                "source": "lme_embedding_sel_v1_pair_gold",
                "source_pair_id": source_id,
                "language": "English",
                "split": lme_split[source_id],
                "label": "negative",
                "source_label": "non_equivalent",
                "provisional": bool(row.get("provisional", True)),
                "left": row["left"],
                "right": row["right"],
                "embedding_text": {
                    "semantics": "frozen natural claim index_text",
                    "provenance": "precomputed scores from lme_embedding_sel_v1_pair_gold.jsonl",
                },
                "scores": {code: float(row["scores"][code]) for code in CONFIG_CODES},
            }
        )
    return sorted(frozen, key=lambda row: str(row["pair_id"]))


def _negative_metrics(
    rows: Sequence[Mapping[str, Any]],
    code: str,
    threshold: float,
    *,
    language: str | None = None,
) -> dict[str, Any]:
    selected = [row for row in rows if row["label"] == "negative" and (language is None or row["language"] == language)]
    false_ids = [str(row["pair_id"]) for row in selected if float(row["scores"][code]) >= threshold]
    false_count = len(false_ids)
    total = len(selected)
    return {
        "fp": false_count,
        "tn": total - false_count,
        "negative_total": total,
        "empirical_false_merge_rate": false_count / total if total else None,
        "false_merge_upper_95_one_sided": clopper_pearson_upper(false_count, total),
        "false_merge_pair_ids": false_ids,
    }


def _equivalent_metrics(rows: Sequence[Mapping[str, Any]], code: str, threshold: float) -> dict[str, Any]:
    selected = [row for row in rows if row["label"] == "equivalent"]
    true_ids = [str(row["pair_id"]) for row in selected if float(row["scores"][code]) >= threshold]
    false_ids = [str(row["pair_id"]) for row in selected if float(row["scores"][code]) < threshold]
    return {
        "tp": len(true_ids),
        "fn": len(false_ids),
        "equivalent_total": len(selected),
        "equivalent_recall": len(true_ids) / len(selected) if selected else None,
        "true_positive_pair_ids": true_ids,
        "false_negative_pair_ids": false_ids,
    }


def _dataset_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for source in sorted({str(row["source"]) for row in rows}):
        source_rows = [row for row in rows if row["source"] == source]
        summary[source] = {
            "rows": len(source_rows),
            "dev": sum(row["split"] == "dev" for row in source_rows),
            "validation": sum(row["split"] == "validation" for row in source_rows),
            "equivalent": sum(row["label"] == "equivalent" for row in source_rows),
            "negative": sum(row["label"] == "negative" for row in source_rows),
            "language": sorted({str(row["language"]) for row in source_rows}),
            "provisional_rows": sum(bool(row["provisional"]) for row in source_rows),
        }
    return summary


def _evaluate(frozen: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    dev = [row for row in frozen if row["split"] == "dev"]
    validation = [row for row in frozen if row["split"] == "validation"]
    dev_labels = [row["label"] == "equivalent" for row in dev]
    validation_labels = [row["label"] == "equivalent" for row in validation]
    validation_ids = [str(row["pair_id"]) for row in validation]

    configs: dict[str, Any] = {}
    predictions: dict[str, list[bool]] = {}
    for code in CONFIG_CODES:
        dev_scores = [float(row["scores"][code]) for row in dev]
        validation_scores = [float(row["scores"][code]) for row in validation]
        selected = select_safe_threshold(dev_labels, dev_scores)
        threshold = float(selected["threshold"])
        validation_overall = metrics_at_threshold(validation_labels, validation_scores, threshold)
        negative_overall = _negative_metrics(validation, code, threshold)
        negative_by_language = {
            language: _negative_metrics(validation, code, threshold, language=language)
            for language in ("Chinese", "English")
        }
        safety_pass = negative_overall["fp"] == 0 and all(
            metrics["fp"] == 0 for metrics in negative_by_language.values()
        )
        configs[code] = {
            "threshold": threshold,
            "dev_selection": selected,
            "validation": {
                "overall": validation_overall,
                "negative_overall": negative_overall,
                "negative_by_language": negative_by_language,
                "equivalent": _equivalent_metrics(validation, code, threshold),
                "observed_safety_target_pass": safety_pass,
            },
            "fixed_observation_points": {
                f"{fixed:g}": {
                    "dev": metrics_at_threshold(dev_labels, dev_scores, fixed),
                    "validation": metrics_at_threshold(validation_labels, validation_scores, fixed),
                }
                for fixed in FIXED_OBSERVATION_THRESHOLDS
            },
        }
        predictions[code] = [score >= threshold for score in validation_scores]

    pairwise: dict[str, Any] = {}
    for code_a, code_b in itertools.combinations(CONFIG_CODES, 2):
        pairwise[f"{code_a}_vs_{code_b}"] = pairwise_comparison(
            validation_ids,
            validation_labels,
            predictions[code_a],
            predictions[code_b],
        )

    relative_to_q1: dict[str, Any] = {}
    blind_candidates: list[str] = []
    for code in ("Q0", "Q2"):
        comparison = pairwise_comparison(
            validation_ids,
            validation_labels,
            predictions[code],
            predictions["Q1"],
        )
        net_gain = int(comparison["equivalent_pairs"]["net_wins"])
        safety_pass = bool(configs[code]["validation"]["observed_safety_target_pass"])
        if not safety_pass:
            outcome = "fails_safety"
        elif net_gain >= 2:
            outcome = "win"
            blind_candidates.append(code)
        elif net_gain <= -2:
            outcome = "loss"
        else:
            outcome = "tie"
        relative_to_q1[code] = {
            "outcome": outcome,
            "observed_safety_target_pass": safety_pass,
            "equivalent_net_gain_vs_q1": net_gain,
            "win_rule": "net_gain_ge_2",
            "comparison": comparison,
        }

    decision = {
        "baseline": "Q1",
        "relative_to_q1": relative_to_q1,
        "conditional_blind_review": {
            "triggered": bool(blind_candidates),
            "candidate_configs": blind_candidates,
            "reason": (
                "at least one candidate passed observed safety and gained at least two validation equivalent pairs"
                if blind_candidates
                else "no candidate both passed observed safety and gained at least two validation equivalent pairs"
            ),
        },
        "recommendation": "conditional_blind_review" if blind_candidates else "keep_Q1",
    }
    return configs, pairwise, decision


def _format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def _render_report(payload: Mapping[str, Any]) -> str:
    configs = payload["configs"]
    pairwise = payload["pairwise_validation"]
    decision = payload["decision"]
    dataset = payload["frozen_data"]["summary"]
    lines = [
        "# HL-Mem embedding 独立阈值校准报告（Q0/Q1/Q2）",
        "",
        f"生成时间：{payload['generated_at']}  ",
        f"项目 HEAD：`{payload['project']['git_head']}`  ",
        "状态：共识方案第 1–3 步已执行；未修改生产代码或生产阈值。",
        "",
        "## 1. 冻结口径",
        "",
        "- 正类仅为 `equivalent`；`compatible/conflict/unrelated/uncertain/non_equivalent` 均按不可自动合并的 negative 处理。",
        "- 判定语义为 `cosine >= threshold` 即 merge；Q0/Q1/Q2 使用完全相同的 pair、label 和 split。",
        '- 旧 80 pair 保留原 dev/test，原 test 映射为 validation；LME 100 English negative 按 `SHA-256("lme:" + pair_id)` 排序后固定 50/50。',
        "- 阈值候选仅为 dev 全部唯一分数的相邻中点，加一个高于最大分数的安全边界；0.82/0.92/0.95 仅作观察点。",
        "- 选择规则：dev observed FP=0 → equivalent TP 最大 → TP 相同时阈值更高者优先。validation 不参与选阈值。",
        "",
        "| 数据源 | 语言 | 总数 | dev | validation | equivalent | negative | provisional |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for source, summary in dataset.items():
        lines.append(
            f"| {source} | {', '.join(summary['language'])} | {summary['rows']} | {summary['dev']} | "
            f"{summary['validation']} | {summary['equivalent']} | {summary['negative']} | "
            f"{summary['provisional_rows']} |"
        )

    lines.extend(
        [
            "",
            f"冻结 pair manifest fingerprint：`{payload['frozen_data']['pair_manifest_sha256']}`",
            f"冻结 score manifest fingerprint：`{payload['frozen_data']['pair_score_manifest_sha256']}`",
            "",
            "## 2. 校准阈值与 validation",
            "",
            "95% 上界为 exact Clopper–Pearson 单侧上界；FP/TN 均为原始计数。",
            "",
            "| 配置 | 冻结阈值 | dev TP/eq | dev FP/TN | val 总体 FP/TN | 总体 rate / 上界 | English FP/TN / 上界 | 中文 FP/TN / 上界 | val eq TP/总数 | 安全目标 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for code in CONFIG_CODES:
        config = configs[code]
        dev = config["dev_selection"]["metrics"]
        validation = config["validation"]
        overall = validation["negative_overall"]
        english = validation["negative_by_language"]["English"]
        chinese = validation["negative_by_language"]["Chinese"]
        equivalent = validation["equivalent"]
        lines.append(
            f"| {code} | {config['threshold']:.9f} | {dev['tp']}/{dev['equivalent_total']} | "
            f"{dev['fp']}/{dev['tn']} | {overall['fp']}/{overall['tn']} | "
            f"{_format_rate(overall['empirical_false_merge_rate'])} / "
            f"{_format_rate(overall['false_merge_upper_95_one_sided'])} | "
            f"{english['fp']}/{english['tn']} / {_format_rate(english['false_merge_upper_95_one_sided'])} | "
            f"{chinese['fp']}/{chinese['tn']} / {_format_rate(chinese['false_merge_upper_95_one_sided'])} | "
            f"{equivalent['tp']}/{equivalent['equivalent_total']} | "
            f"{'通过' if validation['observed_safety_target_pass'] else '⚠️ 未通过'} |"
        )

    lines.extend(
        [
            "",
            "## 3. 固定观察点（不参与选阈值）",
            "",
            "| 配置 | 阈值 | dev FP/negative | dev eq TP/总数 | val FP/negative | val eq TP/总数 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for code in CONFIG_CODES:
        for fixed in FIXED_OBSERVATION_THRESHOLDS:
            observation = configs[code]["fixed_observation_points"][f"{fixed:g}"]
            dev = observation["dev"]
            validation = observation["validation"]
            lines.append(
                f"| {code} | {fixed:.2f} | {dev['fp']}/{dev['negative_total']} | "
                f"{dev['tp']}/{dev['equivalent_total']} | {validation['fp']}/{validation['negative_total']} | "
                f"{validation['tp']}/{validation['equivalent_total']} |"
            )

    lines.extend(
        [
            "",
            "## 4. 配置间逐 pair 净胜负",
            "",
            "胜/负均从左侧配置视角计算；all pair 比较分类正确性，equivalent 比较是否捕获正例。完整 pair ID 明细见 JSON。",
            "",
            "| 对比 | all pair 胜/负/平（净） | equivalent 胜/负/平（净） |",
            "|---|---:|---:|",
        ]
    )
    for key, label in (
        ("Q0_vs_Q1", "Q0 vs Q1"),
        ("Q0_vs_Q2", "Q0 vs Q2"),
        ("Q1_vs_Q2", "Q1 vs Q2"),
    ):
        all_pairs = pairwise[key]["all_pairs"]
        equivalent = pairwise[key]["equivalent_pairs"]
        lines.append(
            f"| {label} | {all_pairs['wins']}/{all_pairs['losses']}/{all_pairs['ties']} "
            f"({all_pairs['net_wins']:+d}) | {equivalent['wins']}/{equivalent['losses']}/{equivalent['ties']} "
            f"({equivalent['net_wins']:+d}) |"
        )

    lines.extend(
        [
            "",
            "### 4.1 相对 Q1 的门禁判定",
            "",
            "equivalent 净增益 ≥2 才算赢，±1 均按持平。",
            "",
            "| 配置 vs Q1 | equivalent 净增益 | 安全 | 判定 |",
            "|---|---:|---|---|",
        ]
    )
    for code in ("Q0", "Q2"):
        result = decision["relative_to_q1"][code]
        equivalent = result["comparison"]["equivalent_pairs"]
        outcome_text = {
            "win": "赢",
            "tie": "持平",
            "loss": "负",
            "fails_safety": "⚠️ 安全门失败",
        }[result["outcome"]]
        lines.append(
            f"| {code} vs Q1 | {equivalent['net_wins']:+d} | "
            f"{'通过' if result['observed_safety_target_pass'] else '未通过'} | {outcome_text} |"
        )

    triggered = decision["conditional_blind_review"]["triggered"]
    candidates = ", ".join(decision["conditional_blind_review"]["candidate_configs"]) or "无"
    outcome_names = {
        "win": "赢",
        "tie": "持平",
        "loss": "负",
        "fails_safety": "安全门失败",
    }
    lines.extend(
        [
            "",
            "## 5. 判定结论",
            "",
            f"- Q0 vs Q1：**{outcome_names[decision['relative_to_q1']['Q0']['outcome']]}**，equivalent 净增益 "
            f"{decision['relative_to_q1']['Q0']['equivalent_net_gain_vs_q1']:+d}。",
            f"- Q2 vs Q1：**{outcome_names[decision['relative_to_q1']['Q2']['outcome']]}**，equivalent 净增益 "
            f"{decision['relative_to_q1']['Q2']['equivalent_net_gain_vs_q1']:+d}。",
            f"- 条件性盲审：**{'触发' if triggered else '不触发'}**；候选：{candidates}。",
            f"- 当前建议：**{'进入候选盲审，盲审前不切生产' if triggered else '保持生产 Q1'}**。",
            "",
            "## 6. 限制与解释",
            "",
            "- ⚠️ LME 100 English negative 是 `qwen3.7-plus` 辅助判定的 provisional label，不是完整人工 gold；绝对 false-merge rate 只能按该冻结标签解释。",
            "- ⚠️ LME negative 是由五配置高 cosine 候选池筛出的压力集；本 validation 是该压力集的冻结内部 holdout，不是无选择偏差的线上总体估计。",
            "- validation observed FP=0 只表示本样本未观察到误合并，不表示真实风险为零；单侧 95% 上界量化了有限样本不确定性。",
            "- 本报告只校准 semantic-dedup cosine 阈值，不替代此前 dense recall 评测，也未更改生产配置。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _git_head() -> str:
    head_path = ROOT / ".git" / "HEAD"
    if not head_path.exists():
        return "unknown"
    head = head_path.read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        reference = ROOT / ".git" / head[5:]
        if reference.exists():
            return reference.read_text(encoding="utf-8").strip()[:12]
    return head[:12]


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    claim_rows = read_jsonl(arguments.claim_pairs)
    lme_rows = read_jsonl(arguments.lme_pairs)
    _validate_inputs(claim_rows, lme_rows)
    old_scores, cache_usage = _old_pair_scores(claim_rows, arguments.cache_dir, allow_api=arguments.allow_api)
    frozen = _freeze_pairs(claim_rows, lme_rows, old_scores)
    configs, pairwise, decision = _evaluate(frozen)

    pair_manifest = [
        {
            "pair_id": row["pair_id"],
            "source": row["source"],
            "source_pair_id": row["source_pair_id"],
            "language": row["language"],
            "split": row["split"],
            "label": row["label"],
            "source_label": row["source_label"],
            "provisional": row["provisional"],
            "left": row["left"],
            "right": row["right"],
        }
        for row in frozen
    ]
    score_manifest = [{"pair_id": row["pair_id"], "scores": row["scores"]} for row in frozen]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "completed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project": {"version": "0.24.1", "git_head": _git_head()},
        "method": {
            "configs": list(CONFIG_CODES),
            "merge_comparison": "cosine >= threshold",
            "positive_label": "equivalent",
            "negative_labels": ["compatible", "conflict", "unrelated", "uncertain", "non_equivalent"],
            "threshold_selection": "dev_fp_eq_0_then_max_equivalent_tp_then_max_threshold",
            "threshold_candidates": "all_unique_score_midpoints_plus_upper_boundary",
            "fixed_observation_thresholds": list(FIXED_OBSERVATION_THRESHOLDS),
            "validation_tuning_forbidden": True,
            "confidence_interval": "exact Clopper-Pearson one-sided 95% upper",
            "winner_rule_vs_q1": "validation equivalent net gain >= 2 and observed safety pass",
        },
        "inputs": {
            "claim_pairs": {
                "path": str(arguments.claim_pairs),
                "sha256": _sha256_file(arguments.claim_pairs),
            },
            "lme_pairs": {
                "path": str(arguments.lme_pairs),
                "sha256": _sha256_file(arguments.lme_pairs),
            },
            "embedding_cache": str(arguments.cache_dir),
        },
        "frozen_data": {
            "pair_count": len(frozen),
            "dev_count": sum(row["split"] == "dev" for row in frozen),
            "validation_count": sum(row["split"] == "validation" for row in frozen),
            "summary": _dataset_summary(frozen),
            "lme_split_algorithm": "sort SHA-256('lme:' + pair_id), first floor(n/2) dev, remainder validation",
            "pair_manifest_sha256": _canonical_sha256(pair_manifest),
            "pair_score_manifest_sha256": _canonical_sha256(score_manifest),
        },
        "cache_usage": cache_usage,
        "configs": configs,
        "pairwise_validation": pairwise,
        "decision": decision,
        "pairs": frozen,
    }
    _write_text_atomic(arguments.output, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write_text_atomic(arguments.report, _render_report(payload))
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-pairs", type=Path, default=DEFAULT_CLAIM_PAIRS)
    parser.add_argument("--lme-pairs", type=Path, default=DEFAULT_LME_PAIRS)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--allow-api",
        action="store_true",
        help="allow EMBEDDING_API_KEY calls to replenish missing old-pair cache batches",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    payload = run(arguments)
    total_network_calls = sum(
        int(metrics["network_api_calls_this_run"]) for metrics in payload["cache_usage"]["configs"].values()
    )
    decision_summary = {
        code: {
            "outcome": payload["decision"]["relative_to_q1"][code]["outcome"],
            "observed_safety_target_pass": payload["decision"]["relative_to_q1"][code]["observed_safety_target_pass"],
            "equivalent_net_gain_vs_q1": payload["decision"]["relative_to_q1"][code]["equivalent_net_gain_vs_q1"],
        }
        for code in ("Q0", "Q2")
    }
    print(
        json.dumps(
            {
                "status": payload["status"],
                "output": str(arguments.output),
                "report": str(arguments.report),
                "network_api_calls_this_run": total_network_calls,
                "thresholds": {code: payload["configs"][code]["threshold"] for code in CONFIG_CODES},
                "decision": {
                    "relative_to_q1": decision_summary,
                    "conditional_blind_review": payload["decision"]["conditional_blind_review"],
                    "recommendation": payload["decision"]["recommendation"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
