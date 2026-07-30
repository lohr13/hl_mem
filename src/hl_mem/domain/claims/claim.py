"""Claim 展示文本与索引文本的领域构造。"""

from __future__ import annotations

import re
from typing import Any, Literal

from hl_mem.domain.claims.attributes import (
    SLOT_REGISTRY,
    normalize_canonical_attribute,
    normalize_predicate,
)

IndexTextMode = Literal["legacy", "value_only", "natural", "answerable"]


def _normalize_answerable_text(value: object) -> str:
    """折叠原子字符串的空白，不渲染其他 JSON 类型。"""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _answerable_value(value: object) -> str:
    """精确解包历史 supersede envelope，其他对象不参与文本投影。"""
    if isinstance(value, dict) and value.get("_type") == "superseded_value":
        value = value.get("old_value")
    return _normalize_answerable_text(value)


def _required_qualifier_phrases(
    claim: dict[str, Any],
    required_qualifiers: tuple[str, ...],
) -> tuple[str, ...]:
    """按 registry 声明顺序构造稳定的必需 qualifier 短语。"""
    qualifiers = claim.get("qualifiers")
    if not isinstance(qualifiers, dict):
        return ()
    phrases: list[str] = []
    for key in required_qualifiers:
        value = _normalize_answerable_text(qualifiers.get(key))
        if value:
            phrases.append(f"{key}: {value}")
    return tuple(phrases)


def build_index_text(claim: dict[str, Any], mode: IndexTextMode = "legacy") -> str:
    """按配置模式生成独立索引文本。"""
    if mode not in {"legacy", "value_only", "natural", "answerable"}:
        raise ValueError(f"unsupported index_text mode: {mode}")
    if mode == "answerable":
        subject = _normalize_answerable_text(claim.get("subject_entity_id"))
        canonical_slot = normalize_canonical_attribute(str(claim.get("canonical_slot") or ""))
        slot = SLOT_REGISTRY.get(canonical_slot)
        if slot is None or canonical_slot == "custom.unknown":
            label = _normalize_answerable_text(normalize_predicate(str(claim.get("predicate") or "")))
            qualifier_phrases: tuple[str, ...] = ()
        else:
            label = _normalize_answerable_text(slot.description)
            qualifier_phrases = _required_qualifier_phrases(claim, slot.required_qualifiers)
        answerable_value = _answerable_value(claim.get("value"))
        return " ".join(
            part
            for part in (
                subject,
                label,
                *qualifier_phrases,
                answerable_value,
            )
            if part
        )

    value = str(claim.get("value") if claim.get("value") is not None else "").strip()
    if mode == "value_only":
        return value
    if mode == "natural":
        subject = str(claim.get("subject_entity_id") or "").strip()
        return f"{subject}：{value}" if subject and value else subject or value

    tags = " ".join(str(tag) for tag in claim.get("topic_tags", []) if tag)
    return " ".join(
        str(part).strip()
        for part in (
            claim.get("subject_entity_id"),
            claim.get("predicate"),
            claim.get("value"),
            claim.get("canonical_slot"),
            tags,
        )
        if part is not None and str(part).strip()
    )
