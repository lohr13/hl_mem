#!/usr/bin/env python
"""将 extraction benchmark 结果与人工 gold 标注逐模型对比。"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from hl_mem.domain.claims.attributes import (
    SLOT_REGISTRY,
    normalize_canonical_attribute,
    normalize_predicate,
    predicate_for_canonical_attribute,
)
from hl_mem.domain.entity import load_entity_aliases, normalize_entity_id

SCRIPT_DIR = Path(__file__).resolve().parent

_EVALUATION_ENTITY_ALIASES = {
    **load_entity_aliases(),
    "user": "用户",
    "用户": "用户",
}
_CANONICAL_FAMILY_PREDICATES = {
    "choice": frozenset({"使用", "配置"}),
    "config": frozenset({"使用", "配置"}),
    "fact": frozenset({"事实", "状态"}),
    "state": frozenset({"事实", "状态"}),
}
_NUMBER_PATTERN = re.compile(r"(?<![\w.])(?P<number>[+-]?(?:(?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.\d+)?)(?![\w.])")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。！？；]+", flags=re.IGNORECASE)


@dataclass(frozen=True)
class ClaimMatch:
    """一对通过 subject、predicate 和 value 语义检查的 claim。"""

    gold_index: int
    predicted_index: int
    value_score: float


def parse_args() -> argparse.Namespace:
    """解析 gold、benchmark 路径与 value 匹配阈值。"""
    parser = argparse.ArgumentParser(description="按模型评估 extraction benchmark 的 gold 指标")
    parser.add_argument("--gold", type=Path, default=SCRIPT_DIR / "gold_dataset.jsonl")
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=SCRIPT_DIR / "extraction_benchmark_results.jsonl",
    )
    parser.add_argument("--value-threshold", type=float, default=0.62)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL；允许 benchmark 参数指向含 results.jsonl 的运行目录。"""
    resolved = path / "results.jsonl" if path.is_dir() else path
    if not resolved.is_file():
        raise FileNotFoundError(f"找不到 JSONL 文件：{resolved}")
    return [json.loads(line) for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize_text(value: Any) -> str:
    """规范化中英文文本，消除空白与标点差异。"""
    text = normalize_value(value)
    text = _NUMBER_PATTERN.sub(_encode_number, text)
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def normalize_subject(value: Any) -> str:
    """复用生产实体规则，并统一评测数据中的用户角色标签。"""
    subject = None if value is None else str(value)
    return normalize_entity_id(subject, aliases=_EVALUATION_ENTITY_ALIASES).casefold()


def canonical_predicate_labels(claim: dict[str, Any]) -> frozenset[str]:
    """返回字面 predicate 及 canonical attribute 投影出的允许标签集合。"""
    predicate = normalize_predicate(str(claim.get("predicate") or ""))
    attribute = claim.get("canonical_attribute") or claim.get("canonical_slot")
    projected = predicate_for_canonical_attribute(attribute, predicate)
    labels = {label for label in (predicate, projected) if label}
    normalized_attribute = normalize_canonical_attribute(str(attribute or ""))
    if normalized_attribute in SLOT_REGISTRY:
        family = normalized_attribute.partition(".")[0]
        labels.update(_CANONICAL_FAMILY_PREDICATES.get(family, ()))
    return frozenset(labels)


def _normalize_number(match: re.Match[str]) -> str:
    """把千分位、前导零和无意义的小数零归一为同一数字文本。"""
    raw = match.group("number").replace(",", "")
    integer, separator, fraction = raw.partition(".")
    sign = "-" if integer.startswith("-") else ""
    unsigned_integer = integer.lstrip("+-")
    normalized_integer = str(int(unsigned_integer or "0"))
    normalized_fraction = fraction.rstrip("0") if separator else ""
    if normalized_integer == "0" and not normalized_fraction:
        sign = ""
    suffix = f".{normalized_fraction}" if normalized_fraction else ""
    return f"{sign}{normalized_integer}{suffix}"


def _encode_number(match: re.Match[str]) -> str:
    """把数字编码为不会在去标点时丢失符号或小数点的 token。"""
    normalized = _normalize_number(match)
    sign = "negative" if normalized.startswith("-") else ""
    magnitude = normalized.lstrip("-").replace(".", "point")
    return f" number{sign}{magnitude} "


def normalize_value(value: Any) -> str:
    """规范 value 的 Unicode、URL 尾斜杠、数字格式与空白。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = _URL_PATTERN.sub(lambda match: match.group(0).rstrip("/"), text)
    text = _NUMBER_PATTERN.sub(_normalize_number, text)
    return re.sub(r"\s+", " ", text).strip()


def normalized_numbers(value: Any) -> frozenset[str]:
    """提取规范化后的数字集合，用于阻止数值冲突被文本相似度掩盖。"""
    normalized = normalize_value(value)
    return frozenset(_normalize_number(match) for match in _NUMBER_PATTERN.finditer(normalized))


def value_similarity(left: Any, right: Any) -> float:
    """以包含关系、序列相似度和字符二元组衡量短文本语义近似。"""
    left_numbers = normalized_numbers(left)
    right_numbers = normalized_numbers(right)
    if left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers):
        return 0.0
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return float(normalized_left == normalized_right)
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return min(len(normalized_left), len(normalized_right)) / max(len(normalized_left), len(normalized_right))
    sequence_score = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    left_bigrams = {normalized_left[index : index + 2] for index in range(max(len(normalized_left) - 1, 1))}
    right_bigrams = {normalized_right[index : index + 2] for index in range(max(len(normalized_right) - 1, 1))}
    union = left_bigrams | right_bigrams
    jaccard_score = len(left_bigrams & right_bigrams) / len(union) if union else 0.0
    return max(sequence_score, jaccard_score)


def match_claims(
    gold_claims: list[dict[str, Any]],
    predicted_claims: list[dict[str, Any]],
    *,
    value_threshold: float,
) -> list[ClaimMatch]:
    """贪心选择互不重复的最高分 claim 对。"""
    candidates: list[ClaimMatch] = []
    for gold_index, gold in enumerate(gold_claims):
        for predicted_index, predicted in enumerate(predicted_claims):
            if normalize_subject(gold.get("subject")) != normalize_subject(predicted.get("subject")):
                continue
            if not canonical_predicate_labels(gold) & canonical_predicate_labels(predicted):
                continue
            score = value_similarity(gold.get("value"), predicted.get("value"))
            if score >= value_threshold:
                candidates.append(ClaimMatch(gold_index, predicted_index, score))

    matches: list[ClaimMatch] = []
    used_gold: set[int] = set()
    used_predicted: set[int] = set()
    for candidate in sorted(candidates, key=lambda item: item.value_score, reverse=True):
        if candidate.gold_index in used_gold or candidate.predicted_index in used_predicted:
            continue
        matches.append(candidate)
        used_gold.add(candidate.gold_index)
        used_predicted.add(candidate.predicted_index)
    return matches


def evaluate_model(
    gold_records: list[dict[str, Any]],
    model_results: list[dict[str, Any]],
    *,
    value_threshold: float,
) -> dict[str, Any]:
    """计算单模型的记忆判定、claim、scope 和分布指标。"""
    results_by_event = {result["event_id"]: result for result in model_results}
    should_matches = 0
    matched_claims = 0
    gold_count = 0
    predicted_count = 0
    scope_matches = 0
    predicate_counts: Counter[str] = Counter()
    evaluated_events = 0

    for gold_record in gold_records:
        result = results_by_event.get(gold_record["event_id"])
        if result is None:
            continue
        evaluated_events += 1
        predicted_claims = result.get("claims_data") or []
        predicted_should_memorize = bool(result.get("should_memorize", predicted_claims))
        should_matches += int(predicted_should_memorize == gold_record["should_memorize"])
        gold_claims = gold_record["gold_claims"]
        matches = match_claims(gold_claims, predicted_claims, value_threshold=value_threshold)
        matched_claims += len(matches)
        gold_count += len(gold_claims)
        predicted_count += len(predicted_claims)
        predicate_counts.update(str(claim.get("predicate", "")) for claim in predicted_claims)
        scope_matches += sum(
            gold_claims[match.gold_index].get("scope") == predicted_claims[match.predicted_index].get("scope")
            for match in matches
        )

    return {
        "events": evaluated_events,
        "should_memorize_accuracy": should_matches / evaluated_events if evaluated_events else 0.0,
        "claim_precision": matched_claims / predicted_count if predicted_count else float(gold_count == 0),
        "claim_recall": matched_claims / gold_count if gold_count else 1.0,
        "scope_accuracy": scope_matches / matched_claims if matched_claims else 0.0,
        "missed": gold_count - matched_claims,
        "over_extracted": predicted_count - matched_claims,
        "predicate_distribution": predicate_counts,
    }


def print_table(stats: dict[str, dict[str, Any]]) -> None:
    """输出紧凑的逐模型 Markdown 对比表。"""
    print("| 模型 | 事件 | should_memorize | claim precision | claim recall | scope accuracy | 漏提取 | 过提取 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for model, values in stats.items():
        print(
            f"| {model} | {values['events']} | {values['should_memorize_accuracy']:.1%} | "
            f"{values['claim_precision']:.1%} | {values['claim_recall']:.1%} | "
            f"{values['scope_accuracy']:.1%} | {values['missed']} | {values['over_extracted']} |"
        )
    print("\nPredicate 分布：")
    for model, values in stats.items():
        distribution = ", ".join(
            f"{predicate or '<empty>'}={count}" for predicate, count in values["predicate_distribution"].most_common()
        )
        print(f"- {model}: {distribution or '<none>'}")


def main() -> None:
    """加载输入、按模型评估并打印对比表。"""
    args = parse_args()
    if not 0.0 <= args.value_threshold <= 1.0:
        raise ValueError("--value-threshold 必须位于 [0, 1]")
    gold_records = load_jsonl(args.gold)
    benchmark_results = load_jsonl(args.benchmark)
    models = list(dict.fromkeys(str(result["model"]) for result in benchmark_results))
    stats = {
        model: evaluate_model(
            gold_records,
            [result for result in benchmark_results if result["model"] == model],
            value_threshold=args.value_threshold,
        )
        for model in models
    }
    print_table(stats)


if __name__ == "__main__":
    main()
