"""召回评测数据集模型、校验和关键词动态绑定。"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable


class BindingError(ValueError):
    """表示评测标签无法在冻结快照中唯一绑定。"""


@dataclass(frozen=True)
class KeywordBinding:
    """用于解析快照实体的稳定文本关键词。"""

    claim_keyword_groups: tuple[tuple[str, ...], ...]
    evidence_keywords: tuple[str, ...] = ()

    @property
    def claim_keywords(self) -> tuple[str, ...]:
        """兼容单组绑定的便捷访问。"""
        return self.claim_keyword_groups[0]


@dataclass(frozen=True)
class EvalCase:
    """一条经过校验、可选已绑定的召回评测样本。"""

    case_id: str
    query: str
    intent: str
    expected_type: str
    expected_min_confidence: float | None
    expected_status_filter: str
    expected_keywords: tuple[str, ...]
    keyword_match: str
    binding: KeywordBinding | None
    forbidden_statuses: tuple[str, ...]
    as_of: str | None = None
    known_as_of: str | None = None
    notes: str = ""
    relevant_claim_ids: tuple[str, ...] = ()
    expected_evidence_event_ids: tuple[str, ...] = ()


def _strings(value: Any, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} 必须是非空字符串数组")
    if not allow_empty and not value:
        raise ValueError(f"{field} 不得为空")
    return tuple(item.strip() for item in value)


def _parse_case(raw: Any, line_number: int) -> EvalCase:
    if not isinstance(raw, dict):
        raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
    case_id, query = raw.get("id"), raw.get("query")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"第 {line_number} 行 id 不得为空")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"{case_id}: query 不得为空")
    expected_type = raw.get("expected_type")
    if expected_type not in {"claim", "empty"}:
        raise ValueError(f"{case_id}: expected_type 必须是 claim 或 empty")
    keyword_match = raw.get("keyword_match", "all")
    if keyword_match not in {"all", "any"}:
        raise ValueError(f"{case_id}: keyword_match 必须是 all 或 any")
    keywords = _strings(raw.get("expected_keywords", []), "expected_keywords")
    binding_raw = raw.get("binding")
    binding = None
    if expected_type == "empty":
        if keywords or binding_raw:
            raise ValueError(f"{case_id}: empty 样本不能声明关键词或 binding")
    else:
        if not isinstance(binding_raw, dict):
            raise ValueError(f"{case_id}: claim 样本必须声明 binding.claim_keywords")
        groups_raw = binding_raw.get("claim_keyword_groups")
        if groups_raw is not None:
            if not isinstance(groups_raw, list) or not groups_raw:
                raise ValueError(f"{case_id}: claim_keyword_groups 不得为空")
            groups = tuple(_strings(group, "claim_keyword_groups", allow_empty=False) for group in groups_raw)
        else:
            groups = (_strings(binding_raw.get("claim_keywords"), "claim_keywords", allow_empty=False),)
        binding = KeywordBinding(
            claim_keyword_groups=groups,
            evidence_keywords=_strings(binding_raw.get("evidence_keywords", []), "evidence_keywords"),
        )
        if not keywords:
            raise ValueError(f"{case_id}: claim 样本 expected_keywords 不得为空")
    confidence = raw.get("expected_min_confidence")
    if confidence is not None and (not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1):
        raise ValueError(f"{case_id}: expected_min_confidence 必须在 0 到 1 之间")
    return EvalCase(
        case_id=case_id.strip(),
        query=query.strip(),
        intent=str(raw.get("intent", "current_state")),
        expected_type=expected_type,
        expected_min_confidence=float(confidence) if confidence is not None else None,
        expected_status_filter=str(raw.get("expected_status_filter", "active")),
        expected_keywords=keywords,
        keyword_match=keyword_match,
        binding=binding,
        forbidden_statuses=_strings(raw.get("forbidden_statuses", []), "forbidden_statuses"),
        as_of=raw.get("as_of"),
        known_as_of=raw.get("known_as_of"),
        notes=str(raw.get("notes", "")),
    )


def load_cases(path: str | Path) -> list[EvalCase]:
    """读取 JSONL 数据集并执行严格的逐行校验。"""
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = _parse_case(json.loads(line), line_number)
        except json.JSONDecodeError as error:
            raise ValueError(f"第 {line_number} 行不是有效 JSON: {error.msg}") from error
        if case.case_id in seen:
            raise ValueError(f"重复的 id: {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise ValueError("评测数据集不能为空")
    return cases


def _normalized_text(values: Iterable[Any]) -> str:
    rendered: list[str] = []
    for value in values:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        rendered.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or ""))
    return unicodedata.normalize("NFKC", " ".join(rendered)).casefold()


def _matches(text: str, keywords: tuple[str, ...], mode: str = "all") -> bool:
    checks = [unicodedata.normalize("NFKC", keyword).casefold() in text for keyword in keywords]
    return all(checks) if mode == "all" else any(checks)


def bind_cases(connection: sqlite3.Connection, cases: list[EvalCase]) -> list[EvalCase]:
    """按内容关键词把样本绑定到快照 claim 及其 event 证据。"""
    claims = [dict(row) for row in connection.execute("SELECT * FROM claims ORDER BY id")]
    bound: list[EvalCase] = []
    for case in cases:
        if case.expected_type == "empty":
            bound.append(case)
            continue
        assert case.binding is not None
        matches_by_id: dict[str, dict[str, Any]] = {}
        for group in case.binding.claim_keyword_groups:
            group_matches = [
                claim
                for claim in claims
                if _matches(
                    _normalized_text(
                        (
                            claim.get("subject_entity_id"), claim.get("predicate"),
                            claim.get("value_json"), claim.get("qualifiers_json"),
                        )
                    ),
                    group,
                )
            ]
            if not group_matches:
                raise BindingError(f"{case.case_id}: 按 claim_keywords 未找到 claim，关键词组={group}")
            matches_by_id.update((str(claim["id"]), claim) for claim in group_matches)
        claim_ids = tuple(sorted(matches_by_id))
        event_ids: tuple[str, ...] = ()
        if case.binding.evidence_keywords:
            rows = connection.execute(
                "SELECT e.id,e.content_json FROM evidence_links l JOIN events e ON e.id=l.evidence_id "
                f"WHERE l.derived_type='claim' AND l.derived_id IN ({','.join('?' for _ in claim_ids)}) "
                "AND l.evidence_type='event' ORDER BY e.id",
                claim_ids,
            ).fetchall()
            selected = [
                str(row["id"])
                for row in rows
                if _matches(_normalized_text((row["content_json"],)), case.binding.evidence_keywords)
            ]
            if not selected:
                raise BindingError(f"{case.case_id}: 已绑定 claim 但未找到匹配的 event 证据")
            event_ids = tuple(selected)
        bound.append(replace(case, relevant_claim_ids=claim_ids, expected_evidence_event_ids=event_ids))
    return bound
