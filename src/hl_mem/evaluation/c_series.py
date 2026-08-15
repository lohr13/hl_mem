"""C-series 关系召回实验的冻结、默认关闭支撑。

本模块只供 ``evaluation/tools`` 使用，不由生产组件工厂导入。实验臂必须由
调用方显式选择；因此导入本模块不会改变任何默认召回行为。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import sqlite3
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from hl_mem.domain.recall import route_query
from hl_mem.recall.relation_expansion import RelationExpansionConfig
from hl_mem.storage.events import EventRepository

PROTOCOL_VERSION = "c-series-relation-protocol-v1"
INTENT_VERSION = "relation-multihop-intent-v1"
SUFFICIENCY_VERSION = "evidence-sufficiency-v1"
SCORER_VERSION = "answer-entity-packet-v1"
ARM_IDS = ("C0", "C1", "C2", "C3", "C4", "C5", "f4")
RELATION_ALLOWLIST = frozenset({"summarizes", "supports", "follows", "about", "derived_from"})
PACKET_TOKEN_BUDGET = 2_000
PACKET_CLAIM_LIMIT = 10
PATH_TOKEN_BUDGET = 800
PATH_CLAIM_LIMIT = 4
RAW_TOKEN_BUDGET = 800
RAW_RECORD_LIMIT = 6
PLANNER_INPUT_BUDGET = 1_200
PLANNER_OUTPUT_BUDGET = 256
PLANNER_TIMEOUT_SECONDS = 2.0

_RELATION_RE = re.compile(
    r"(?:谁(?:的|是)|负责(?:人|的)|导师|常驻|所在(?:地|城市)|属于|隶属|拥有|报道|"
    r"推荐.{0,16}(?:执行|购买|采用)|执行.{0,16}(?:推荐|方案)|获奖者|项目.{0,12}城市)"
)
_TWO_HOP_RE = re.compile(r"(?:负责人|导师|获奖者|推荐人|所有者).{0,18}(?:哪里|哪座|谁|什么|是否|了吗)")
_ENUMERATION_RE = re.compile(r"(?:全部|所有|完整列出|分别|一共有多少|总共有多少)")
_CURRENT_CONFLICT_RE = re.compile(r"(?:现在|当前|最新|目前).{0,24}(?:值|版本|地址|负责人|状态|使用|是)")
_REQUIRED_RAO_RE = {
    "role": re.compile(r"(?:谁|负责人|导师|获奖者|推荐人|所有者|报道者|项目|经理|用户|他|她)"),
    "action": re.compile(r"(?:推荐|执行|购买|采用|负责|拥有|报道|属于|隶属|参加|获得|使用|迁移|更新)"),
    "object": re.compile(r"(?:什么|哪个|哪些|哪里|城市|方案|产品|项目|奖项|版本|地址|状态|人)"),
}
_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,31}|[\u4e00-\u9fff]{2,12}|\d+(?:\.\d+)?(?:年|月|日|版|岁)?")
_ENTITY_STOP = frozenset(
    {
        "什么",
        "哪个",
        "哪些",
        "哪里",
        "现在",
        "当前",
        "最新",
        "目前",
        "全部",
        "所有",
        "完整列出",
        "一共有多少",
        "总共有多少",
        "负责人",
        "推荐人",
        "所有者",
    }
)


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    relation_enabled: bool
    intent_gated: bool
    max_depth: int
    seed_limit: int
    relation_candidate_limit: int
    atomic_path_packing: bool = False
    raw_fallback: bool = False
    planner: bool = False
    packet_token_budget: int = PACKET_TOKEN_BUDGET
    packet_claim_limit: int = PACKET_CLAIM_LIMIT

    def relation_config(self, *, intent_eligible: bool) -> RelationExpansionConfig:
        enabled = self.relation_enabled and (intent_eligible or not self.intent_gated)
        return RelationExpansionConfig(
            enabled=enabled,
            seed_limit=self.seed_limit,
            candidate_limit=self.relation_candidate_limit,
            relation_weight=0.35,
            max_depth=self.max_depth,
            allowed_relations=RELATION_ALLOWLIST,
        )


_ARMS = {
    "C0": ArmSpec("C0", False, False, 0, 0, 0),
    "C1": ArmSpec("C1", True, False, 1, 5, 12),
    "C2": ArmSpec("C2", True, True, 1, 5, 12),
    "C3": ArmSpec("C3", True, True, 2, 5, 20),
    "C4": ArmSpec("C4", True, True, 2, 5, 20, atomic_path_packing=True),
    "C5": ArmSpec("C5", True, True, 2, 5, 20, atomic_path_packing=True, raw_fallback=True),
    "f4": ArmSpec("f4", True, True, 2, 5, 20, atomic_path_packing=True, planner=True),
}


def arm_spec(arm_id: str) -> ArmSpec:
    try:
        return _ARMS[arm_id]
    except KeyError as error:
        raise ValueError(f"unknown C-series arm: {arm_id}") from error


@dataclass(frozen=True)
class IntentDecision:
    eligible: bool
    reasons: tuple[str, ...]
    required_rao: tuple[str, ...]
    version: str = INTENT_VERSION


def relation_multihop_intent_v1(query: str) -> IntentDecision:
    """仅根据 query 的冻结确定性模式判定关系/多跳 intent。"""
    normalized = unicodedata.normalize("NFC", query).strip()
    reasons: list[str] = []
    if route_query(normalized).intent == "relation":
        reasons.append("existing_relation_route")
    for name, pattern in (
        ("relation_pattern", _RELATION_RE),
        ("two_hop", _TWO_HOP_RE),
        ("enumeration", _ENUMERATION_RE),
        ("current_conflict", _CURRENT_CONFLICT_RE),
    ):
        if pattern.search(normalized):
            reasons.append(name)
    required = tuple(name for name, pattern in _REQUIRED_RAO_RE.items() if pattern.search(normalized))
    return IntentDecision(bool(reasons), tuple(dict.fromkeys(reasons)), required)


def extract_query_entities(query: str) -> tuple[str, ...]:
    """抽取在线门控可观察的 query-side NFC token；不使用 gold 或同义词。"""
    result: list[str] = []
    for match in _ENTITY_TOKEN_RE.finditer(unicodedata.normalize("NFC", query)):
        token = match.group(0).strip("，。！？?：:、的了是有在和与")
        if len(token) >= 2 and token not in _ENTITY_STOP:
            result.append(token)
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class SufficiencyDecision:
    answerability_score: float
    rao_completeness: float | None
    entity_retention: float | None
    score: float
    insufficient: bool
    reasons: tuple[str, ...]
    version: str = SUFFICIENCY_VERSION


def evidence_sufficiency_v1(
    *,
    answerability: str,
    required_rao: Sequence[str],
    covered_rao: Sequence[str],
    query_entities: Sequence[str],
    packet_entities: Sequence[str],
) -> SufficiencyDecision:
    """按冻结权重计算 gold-free 证据充分性。"""
    answerability_values = {"supported": 1.0, "low_confidence": 0.5, "no_evidence": 0.0}
    if answerability not in answerability_values:
        raise ValueError(f"unsupported answerability: {answerability}")
    answerability_score = answerability_values[answerability]
    required = set(required_rao)
    covered = set(covered_rao) & required
    rao = len(covered) / len(required) if required else None
    query_set = {unicodedata.normalize("NFC", str(item)) for item in query_entities if str(item)}
    packet_set = {unicodedata.normalize("NFC", str(item)) for item in packet_entities if str(item)}
    entity = len(query_set & packet_set) / len(query_set) if query_set else None
    observed = [(answerability_score, 0.45)]
    if rao is not None:
        observed.append((rao, 0.35))
    if entity is not None:
        observed.append((entity, 0.20))
    score = sum(value * weight for value, weight in observed) / sum(weight for _, weight in observed)
    reasons: list[str] = []
    if answerability == "no_evidence":
        reasons.append("no_evidence")
    if score < 0.70:
        reasons.append("weighted_score")
    if answerability == "low_confidence" and rao is not None and rao < 2 / 3:
        reasons.append("low_confidence_rao")
    return SufficiencyDecision(answerability_score, rao, entity, score, bool(reasons), tuple(reasons))


@dataclass(frozen=True)
class PackedPacket:
    items: tuple[dict[str, Any], ...]
    token_count: int
    atomic_path_claim_ids: tuple[str, ...]


def _tokens(item: Mapping[str, Any]) -> int:
    raw = item.get("token_count")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    text = str(item.get("text") or item.get("value") or "")
    return max(1, math.ceil(len(text) / 2))


def atomic_pack(
    candidates: Sequence[Mapping[str, Any]],
    relation_paths: Sequence[Mapping[str, Any]],
    *,
    token_budget: int = PACKET_TOKEN_BUDGET,
    claim_limit: int = PACKET_CLAIM_LIMIT,
) -> PackedPacket:
    """C4 原子路径打包；路径不适配时严格退化为 C3 排序。"""
    ordered = sorted(
        (dict(item) for item in candidates),
        key=lambda item: (int(item.get("rank", 10**9)), str(item.get("claim_id", ""))),
    )
    by_id = {str(item.get("claim_id")): item for item in ordered}
    paths = sorted(
        relation_paths,
        key=lambda path: (
            -float(path.get("expansion_score", 0.0)),
            len(path.get("claim_ids") or []),
            tuple(str(item) for item in path.get("claim_ids") or []),
        ),
    )
    reserved: list[dict[str, Any]] = []
    path_ids: tuple[str, ...] = ()
    for path in paths:
        ids = tuple(dict.fromkeys(str(item) for item in path.get("claim_ids") or []))
        members = [by_id[item] for item in ids if item in by_id]
        if len(members) != len(ids) or not ids or len(ids) > PATH_CLAIM_LIMIT:
            continue
        if sum(_tokens(item) for item in members) > PATH_TOKEN_BUDGET:
            continue
        reserved = members
        path_ids = ids
        break
    packed = list(reserved)
    used_ids = {str(item.get("claim_id")) for item in packed}
    used_tokens = sum(_tokens(item) for item in packed)
    for item in ordered:
        claim_id = str(item.get("claim_id"))
        item_tokens = _tokens(item)
        if claim_id in used_ids or len(packed) >= claim_limit or used_tokens + item_tokens > token_budget:
            continue
        packed.append(item)
        used_ids.add(claim_id)
        used_tokens += item_tokens
    return PackedPacket(tuple(packed), used_tokens, path_ids)


def rescue_mode(arm_id: str, *, intent: bool, insufficient: bool) -> Literal["raw", "planner"] | None:
    spec = arm_spec(arm_id)
    if not (intent and insufficient):
        return None
    if spec.raw_fallback:
        return "raw"
    if spec.planner:
        return "planner"
    return None


def select_raw_events(
    connection: sqlite3.Connection,
    *,
    query: str,
    namespace: str,
    question_at: str | None,
    known_as_of: str | None,
    linked_event_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], ...]:
    """C5 同 namespace、双时间约束的 linked-evidence + FTS raw 选择。"""
    repository = EventRepository(connection)
    fts = repository.search_events_fts(query, limit=20)
    linked = set(str(item) for item in linked_event_ids)
    candidates: dict[str, dict[str, Any]] = {}
    if linked:
        placeholders = ",".join("?" for _ in linked)
        rows = connection.execute(
            f"SELECT * FROM events WHERE id IN ({placeholders})", tuple(sorted(linked))
        ).fetchall()
        candidates.update({str(row["id"]): dict(row) for row in rows})
    for fts_rank, item in enumerate(fts):
        candidates.setdefault(str(item["id"]), {**item, "_fts_rank": fts_rank})

    def visible(event: Mapping[str, Any]) -> bool:
        if str(event.get("tenant_id") or "default") != namespace:
            return False
        occurred = str(event.get("occurred_at") or "")
        recorded = str(event.get("recorded_at") or event.get("created_at") or "")
        return (not question_at or not occurred or occurred <= question_at) and (
            not known_as_of or not recorded or recorded <= known_as_of
        )

    eligible = [item for item in candidates.values() if visible(item)]
    eligible.sort(
        key=lambda item: (
            0 if str(item.get("id")) in linked else 1,
            int(item.get("_fts_rank", 10**9)),
            str(item.get("occurred_at") or ""),
            str(item.get("id") or ""),
        )
    )
    selected: list[dict[str, Any]] = []
    used = 0
    for item in eligible:
        content_json = item.get("content_json")
        raw = str(item.get("content_text") or content_json or "")[:256]
        try:
            content = json.loads(str(content_json)) if content_json else raw
        except json.JSONDecodeError:
            content = raw
        token_count = max(1, math.ceil(len(raw) / 2))
        if used + token_count > RAW_TOKEN_BUDGET:
            continue
        selected.append(
            {
                "event_id": str(item.get("id")),
                "text": raw,
                "token_count": token_count,
                "tenant_id": str(item.get("tenant_id") or ""),
                "event_type": str(item.get("event_type") or "unknown"),
                "occurred_at": str(item.get("occurred_at") or ""),
                "recorded_at": str(item.get("recorded_at") or ""),
                "source_uri": str(item.get("source_uri") or ""),
                "content": content,
            }
        )
        used += token_count
        if len(selected) >= RAW_RECORD_LIMIT:
            break
    return tuple(selected)


def planner_prompt(question: str, seeds: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]) -> str:
    """构建不含 gold/raw 的 f4 冻结输入并硬截断到近似 1,200 tokens。"""
    payload = {
        "question": question,
        "seeds": [{key: item.get(key) for key in ("claim_id", "entities", "slot")} for item in seeds[:5]],
        "visible_edges": [
            {key: item.get(key) for key in ("from_id", "to_id", "relation")}
            for item in edges
            if item.get("relation") in RELATION_ALLOWLIST
        ],
        "instruction": "仅输出JSON：subgoals最多2个；每个subgoal只含query与max_depth(1或2)。禁止回答问题。",
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text[: PLANNER_INPUT_BUDGET * 2]


def parse_planner_output(value: str | Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    payload = json.loads(value) if isinstance(value, str) else dict(value)
    subgoals = payload.get("subgoals")
    if not isinstance(subgoals, list) or len(subgoals) > 2:
        raise ValueError("planner subgoals must be a list with at most two entries")
    result: list[dict[str, Any]] = []
    for item in subgoals:
        if not isinstance(item, Mapping) or set(item) != {"query", "max_depth"}:
            raise ValueError("invalid planner subgoal schema")
        query = str(item["query"]).strip()
        depth = item["max_depth"]
        if not query or depth not in {1, 2}:
            raise ValueError("invalid planner subgoal value")
        result.append({"query": query, "max_depth": depth})
    return tuple(result)


def case_seed(preregistration_id: str, corpus_sha256: str, case_id: str, repeat_index: int) -> int:
    digest = hashlib.sha256(f"{preregistration_id}{corpus_sha256}{case_id}{repeat_index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def arm_order(seed: int) -> tuple[str, ...]:
    return tuple(sorted(ARM_IDS, key=lambda arm: hashlib.sha256(f"{seed}{arm}".encode()).hexdigest()))


def is_retryable_error(error: BaseException) -> bool:
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.casefold():
        return True
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    return status == 429


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ordered_root_hash(items: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(dict(sorted(items.items())), separators=(",", ":")).encode()).hexdigest()


PREREGISTRATION_REQUIRED_FIELDS = frozenset(
    {
        "preregistration_id",
        "protocol_version",
        "git_commit",
        "clean_source",
        "runtime",
        "corpora",
        "cache_files",
        "cache_root_sha256",
        "case_ids",
        "models",
        "prompt_hashes",
        "arms",
        "frozen_rules",
        "scorer_version",
    }
)


def build_preregistration(
    *,
    preregistration_id: str,
    git_commit: str,
    clean_source: bool,
    corpus_paths: Mapping[str, Path],
    cache_paths: Sequence[Path],
    model_snapshot: Mapping[str, Any],
    prompt_hashes: Mapping[str, str],
    case_ids: Sequence[str],
) -> dict[str, Any]:
    corpora = {name: sha256_file(path) for name, path in sorted(corpus_paths.items())}
    cache_files = {str(path.as_posix()): sha256_file(path) for path in sorted(cache_paths)}
    manifest: dict[str, Any] = {
        "preregistration_id": preregistration_id,
        "protocol_version": PROTOCOL_VERSION,
        "git_commit": git_commit,
        "clean_source": clean_source,
        "runtime": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "os": platform.platform(),
            "timezone": "Asia/Shanghai",
            "unicode_normalization": f"NFC/unicodedata-{unicodedata.unidata_version}",
        },
        "corpora": corpora,
        "cache_files": cache_files,
        "cache_root_sha256": ordered_root_hash(cache_files),
        "case_ids": list(case_ids),
        "models": dict(model_snapshot),
        "prompt_hashes": dict(prompt_hashes),
        "arms": {arm: asdict(arm_spec(arm)) for arm in ARM_IDS},
        "frozen_rules": {
            "intent_version": INTENT_VERSION,
            "intent_rule_sha256": hashlib.sha256(
                "|".join(
                    pattern.pattern for pattern in (_RELATION_RE, _TWO_HOP_RE, _ENUMERATION_RE, _CURRENT_CONFLICT_RE)
                ).encode()
            ).hexdigest(),
            "sufficiency_version": SUFFICIENCY_VERSION,
            "sufficiency": {
                "weights": {"A": 0.45, "R": 0.35, "E": 0.20},
                "threshold": 0.70,
                "low_confidence_rao": 2 / 3,
            },
            "relation_allowlist": sorted(RELATION_ALLOWLIST),
            "relation_weight": 0.35,
            "seed_rule": "first64bits(SHA256(preregistration_id||corpus_sha256||case_id||repeat_index))",
            "arm_order_rule": "SHA256(case_seed||arm_id)",
            "top_seed_limit": 5,
            "candidate_floor": 50,
            "final_claim_limit": PACKET_CLAIM_LIMIT,
            "packet_token_budget": PACKET_TOKEN_BUDGET,
            "path_token_budget": PATH_TOKEN_BUDGET,
            "raw_token_budget": RAW_TOKEN_BUDGET,
            "vector_backend": "sqlite_scan",
            "query_expansion": "off",
            "repeats": 3,
        },
        "scorer_version": SCORER_VERSION,
    }
    validate_preregistration(manifest)
    return manifest


def validate_preregistration(manifest: Mapping[str, Any]) -> None:
    missing = PREREGISTRATION_REQUIRED_FIELDS - manifest.keys()
    if missing:
        raise ValueError(f"missing preregistration fields: {sorted(missing)}")
    if manifest.get("protocol_version") != PROTOCOL_VERSION or manifest.get("scorer_version") != SCORER_VERSION:
        raise ValueError("protocol/scorer version mismatch")
    if manifest.get("clean_source") is not True:
        raise ValueError("clean_source must be true before live calls")
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(manifest.get("git_commit") or "")):
        raise ValueError("git_commit must be concrete")
    for group in (manifest.get("corpora"), manifest.get("cache_files"), manifest.get("prompt_hashes")):
        if not isinstance(group, Mapping) or not group:
            raise ValueError("all hash groups must be non-empty")
        if any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in group.values()):
            raise ValueError("all snapshot hashes must be SHA-256")


def completed_run_keys(path: Path) -> set[tuple[str, int, str]]:
    if not path.exists():
        return set()
    keys: set[tuple[str, int, str]] = set()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1 and not text.endswith(("\n", "\r")):
                break
            raise ValueError(f"corrupt JSONL record at line {index + 1}") from error
        if item.get("status") == "complete":
            keys.add((str(item["case_id"]), int(item["repeat_index"]), str(item["arm_id"])))
    return keys


def build_intent_dev_queries() -> tuple[dict[str, Any], ...]:
    """生成 120/120 平衡的、可审计的中文 intent dev 标签集。"""
    positive_templates = (
        "项目{n}负责人的常驻城市是哪里？",
        "完整列出项目{n}所有参加发布会的人。",
        "经理{n}推荐的方案最后执行了吗？",
        "当前服务{n}使用的数据库版本是什么？",
        "奖项{n}获奖者的导师是谁？",
        "报道项目{n}的机构拥有什么产品？",
    )
    negative_templates = (
        "我今天第{n}次喝了什么饮料？",
        "请总结会议记录第{n}段。",
        "天气预报第{n}条说会下雨吗？",
        "把第{n}个数字转换成十六进制。",
        "这篇文章第{n}段的主题是什么？",
        "提醒我第{n}次休息的时间。",
    )
    rows: list[dict[str, Any]] = []
    categories = ("two_hop", "enumeration", "recommendation_execution", "conflict_current", "relation", "ownership")
    for index in range(120):
        rows.append(
            {
                "id": f"intent-positive-{index:03d}",
                "query": positive_templates[index % len(positive_templates)].format(n=index),
                "category": categories[index % len(categories)],
                "needs_relation_or_multihop": True,
                "provenance": {
                    "authoring": "deterministic_design_dev",
                    "label_source": "protocol_rule_review",
                },
            }
        )
        rows.append(
            {
                "id": f"intent-negative-{index:03d}",
                "query": negative_templates[index % len(negative_templates)].format(n=index),
                "category": "negative",
                "needs_relation_or_multihop": False,
                "provenance": {
                    "authoring": "deterministic_design_dev",
                    "label_source": "protocol_rule_review",
                },
            }
        )
    return tuple(rows)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
