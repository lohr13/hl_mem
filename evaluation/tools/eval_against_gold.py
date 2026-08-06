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

ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = ROOT / "evaluation" / "datasets"
RESULTS_DIR = ROOT / "evaluation" / "results"

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
_IP_ENDPOINT_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\d.])")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)(?<![a-z0-9])[a-z]:[\\/][^\s\"'<>，。！？；]+")
_UNIX_PATH_PATTERN = re.compile(r"(?<![\w:])/(?:[\w.-]+/)+[\w.-]+")
_TECHNICAL_TOKEN_PATTERN = re.compile(r"(?i)(?:--[a-z][a-z0-9-]*|[a-z][a-z0-9]*(?:[_.:/-][a-z0-9]+)+|[a-z]{3,})")
_CJK_SEQUENCE_PATTERN = re.compile(r"[\u3400-\u9fff]{2,}")
_HL_MEM_SUBJECT_PATTERN = re.compile(r"(?<![a-z0-9])hl[_-]?mem(?![a-z0-9])", flags=re.IGNORECASE)
_NEGATION_PATTERN = re.compile(
    r"(?i)(?:尚未|未曾|没有|并非|不是|不可|不能|无需|无须|不|未|无)|"
    r"(?<![\w])(?:no|not|never|without|disabled)(?![\w])"
)
_TECHNICAL_STOPWORDS = frozenset(
    {
        "adapter",
        "and",
        "api",
        "are",
        "endpoint",
        "entity",
        "evidence",
        "experience",
        "fact",
        "for",
        "from",
        "gpu",
        "graph",
        "has",
        "hl_mem",
        "http",
        "https",
        "model",
        "plugin",
        "port",
        "provider",
        "proxy",
        "reranker",
        "rrf",
        "sqlite",
        "the",
        "this",
        "ttl",
        "true",
        "user",
        "uses",
        "url",
        "vector",
        "wal",
        "with",
    }
)
_CJK_BIGRAM_STOPWORDS = frozenset(
    {
        "一个",
        "当前",
        "已经",
        "支持",
        "使用",
        "事实",
        "作为",
        "可以",
        "启用",
        "用户",
        "系统",
        "配置",
        "采用",
        "项目",
        "默认",
    }
)
_SEMANTIC_KEYWORD_PATTERNS = (
    ("adapter", re.compile(r"(?i)\b(?:adapter|provider)\b|适配器|提供器")),
    ("api", re.compile(r"(?i)\bapi\b|接口")),
    ("component_factory", re.compile(r"(?i)\bcomponents?(?:\.py)?\b|组件工厂")),
    ("entity_graph", re.compile(r"(?i)\bentity\s*graph\b|实体(?:关系)?图谱")),
    ("evidence", re.compile(r"(?i)\bevidence\b|证据链?")),
    ("experience", re.compile(r"(?i)\bexperience\b|经验通道")),
    ("fts", re.compile(r"(?i)\bfts5?\b|全文检索")),
    ("gpu", re.compile(r"(?i)\bgpu\b|显卡")),
    ("localhost", re.compile(r"(?i)\blocalhost\b|本地回环")),
    ("model", re.compile(r"(?i)\bmodel\b|模型")),
    ("plugin", re.compile(r"(?i)\bplugin\b|插件")),
    ("port", re.compile(r"(?i)\bport\b|端口")),
    ("proxy", re.compile(r"(?i)\bproxy\b|代理")),
    ("reranker", re.compile(r"(?i)\breranker\b|重排(?:器|模型)?")),
    ("rrf", re.compile(r"(?i)\brrf\b|倒数排名融合")),
    ("sqlite", re.compile(r"(?i)\bsqlite\b")),
    ("ttl", re.compile(r"(?i)\bttl\b|生存时间")),
    ("url", re.compile(r"(?i)\b(?:url|endpoint|address)\b|地址")),
    ("vector", re.compile(r"(?i)\bvector\b|向量")),
    ("wal", re.compile(r"(?i)\bwal\b")),
)


@dataclass(frozen=True)
class ClaimMatch:
    """一对通过 subject、predicate 和 value 语义检查的 claim。"""

    gold_index: int
    predicted_index: int
    value_score: float


def parse_args() -> argparse.Namespace:
    """解析 gold、benchmark 路径与 value 匹配阈值。"""
    parser = argparse.ArgumentParser(description="按模型评估 extraction benchmark 的 gold 指标")
    parser.add_argument("--gold", type=Path, default=DATASET_DIR / "gold_dataset.jsonl")
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=RESULTS_DIR / "extraction_benchmark_results.jsonl",
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
    """复用生产实体规则，并将 hl_mem 的组件/适配器名称归到项目主体。"""
    subject = None if value is None else str(value)
    normalized = normalize_entity_id(subject, aliases=_EVALUATION_ENTITY_ALIASES).casefold()
    if _HL_MEM_SUBJECT_PATTERN.search(normalized):
        return "hl_mem"
    return normalized


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


def normalized_ip_hosts(value: Any) -> frozenset[str]:
    """提取不含端口的 IPv4 主机，用于阻止相同端口掩盖 IP 冲突。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return frozenset(match.group(0).partition(":")[0] for match in _IP_ENDPOINT_PATTERN.finditer(text))


def normalized_anchors(value: Any) -> frozenset[str]:
    """提取 URL、IP endpoint 与路径等低歧义技术锚点。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("\\", "/")
    anchors: set[str] = set()
    anchors.update(f"url:{match.group(0).rstrip('/')}" for match in _URL_PATTERN.finditer(text))
    anchors.update(f"ip:{match.group(0)}" for match in _IP_ENDPOINT_PATTERN.finditer(text))
    anchors.update(f"path:{match.group(0).rstrip('/')}" for match in _WINDOWS_PATH_PATTERN.finditer(text))
    anchors.update(f"path:{match.group(0).rstrip('/')}" for match in _UNIX_PATH_PATTERN.finditer(text))
    return frozenset(anchors)


def _semantic_keywords(value: Any) -> frozenset[str]:
    """用确定性中英文 token/二元组近似分词，不引入运行时分词依赖。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    tokens = {
        token.strip("./:")
        for token in _TECHNICAL_TOKEN_PATTERN.findall(text)
        if token.strip("./:") not in _TECHNICAL_STOPWORDS
    }
    for canonical, pattern in _SEMANTIC_KEYWORD_PATTERNS:
        if pattern.search(text):
            tokens.add(canonical)
    if re.search(r"(?i)(?:https?://|(?:\d{1,3}\.){3}\d{1,3}):?[^\s]*:\d+", text):
        tokens.add("port")
    for sequence in _CJK_SEQUENCE_PATTERN.findall(text):
        for index in range(len(sequence) - 1):
            bigram = sequence[index : index + 2]
            if bigram not in _CJK_BIGRAM_STOPWORDS:
                tokens.add(f"zh:{bigram}")
    return frozenset(token for token in tokens if token)


def _keyword_overlap(left: Any, right: Any) -> tuple[float, int]:
    left_tokens = _semantic_keywords(left)
    right_tokens = _semantic_keywords(right)
    if not left_tokens or not right_tokens:
        return 0.0, 0
    common_count = len(left_tokens & right_tokens)
    return common_count / min(len(left_tokens), len(right_tokens)), common_count


def value_similarity(left: Any, right: Any) -> float:
    """融合文本近似、关键词覆盖及数字/路径锚点衡量 claim 等价性。"""
    left_value = normalize_value(left)
    right_value = normalize_value(right)
    if bool(_NEGATION_PATTERN.search(left_value)) != bool(_NEGATION_PATTERN.search(right_value)):
        return 0.0
    left_ip_hosts = normalized_ip_hosts(left)
    right_ip_hosts = normalized_ip_hosts(right)
    if left_ip_hosts and right_ip_hosts and left_ip_hosts.isdisjoint(right_ip_hosts):
        return 0.0
    left_numbers = normalized_numbers(left)
    right_numbers = normalized_numbers(right)
    if left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers):
        return 0.0
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return float(normalized_left == normalized_right)
    length_ratio = min(len(normalized_left), len(normalized_right)) / max(len(normalized_left), len(normalized_right))
    containment_score = 0.0
    if normalized_left in normalized_right or normalized_right in normalized_left:
        containment_score = max(length_ratio, 0.84 if min(len(normalized_left), len(normalized_right)) >= 4 else 0.0)
    sequence_score = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    left_bigrams = {normalized_left[index : index + 2] for index in range(max(len(normalized_left) - 1, 1))}
    right_bigrams = {normalized_right[index : index + 2] for index in range(max(len(normalized_right) - 1, 1))}
    union = left_bigrams | right_bigrams
    jaccard_score = len(left_bigrams & right_bigrams) / len(union) if union else 0.0
    score = max(sequence_score, jaccard_score, containment_score)

    keyword_overlap, common_keywords = _keyword_overlap(left, right)
    if common_keywords >= 2 and keyword_overlap >= 0.5:
        score = max(score, min(0.82, 0.62 + 0.20 * keyword_overlap))

    shared_anchors = normalized_anchors(left) & normalized_anchors(right)
    if shared_anchors:
        score = max(score, 0.86)
    if left_numbers & right_numbers and common_keywords >= 2 and keyword_overlap >= 0.25:
        score = max(score, min(0.82, 0.70 + 0.12 * keyword_overlap))
    return score


def _predicates_compatible(gold: dict[str, Any], predicted: dict[str, Any], value_score: float) -> bool:
    """保留 canonical family 硬约束，仅为有强语义证据的已知描述类差异搭桥。"""
    gold_labels = canonical_predicate_labels(gold)
    predicted_labels = canonical_predicate_labels(predicted)
    if gold_labels & predicted_labels:
        return True
    if "计划" in gold_labels or "计划" in predicted_labels:
        return False

    attributes = {
        normalize_canonical_attribute(str(claim.get("canonical_attribute") or claim.get("canonical_slot") or ""))
        for claim in (gold, predicted)
    }
    descriptive_labels = {"事实", "状态", "配置", "使用", "身份"}
    if not (gold_labels | predicted_labels) <= descriptive_labels:
        return False
    if "fact.architecture" in attributes and value_score >= 0.62:
        return True

    left_value = gold.get("value")
    right_value = predicted.get("value")
    shared_anchors = normalized_anchors(left_value) & normalized_anchors(right_value)
    keyword_overlap, common_keywords = _keyword_overlap(left_value, right_value)
    if shared_anchors and value_score >= 0.70:
        return True
    if {"配置", "身份"} <= gold_labels | predicted_labels:
        shared_keywords = _semantic_keywords(left_value) & _semantic_keywords(right_value)
        if "gpu" in shared_keywords and value_score >= 0.62:
            return True
    return common_keywords >= 3 and keyword_overlap >= 0.75 and value_score >= 0.74


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
            score = value_similarity(gold.get("value"), predicted.get("value"))
            if score >= value_threshold and _predicates_compatible(gold, predicted, score):
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
