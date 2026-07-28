"""Claim 展示文本与索引文本的领域构造。"""

from __future__ import annotations

from typing import Any, Literal

from hl_mem.domain.claims.attributes import SLOT_REGISTRY

IndexTextMode = Literal["legacy", "value_only", "natural", "answerable"]


def build_index_text(claim: dict[str, Any], mode: IndexTextMode = "legacy") -> str:
    """按配置模式生成独立索引文本。"""
    if mode not in {"legacy", "value_only", "natural", "answerable"}:
        raise ValueError(f"unsupported index_text mode: {mode}")
    value = str(claim.get("value") if claim.get("value") is not None else "").strip()
    if mode == "value_only":
        return value
    if mode == "natural":
        subject = str(claim.get("subject_entity_id") or "").strip()
        return f"{subject}：{value}" if subject and value else subject or value
    if mode == "answerable":
        subject = str(claim.get("subject_entity_id") or "").strip()
        slot = SLOT_REGISTRY.get(str(claim.get("canonical_slot") or "").strip())
        label = slot.description.strip() if slot is not None else str(claim.get("predicate") or "").strip()
        return " ".join(part for part in (subject, label, value) if part)

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
