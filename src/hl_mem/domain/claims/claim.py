"""Claim 展示文本与索引文本的领域构造。"""

from __future__ import annotations

from typing import Any


def build_index_text(claim: dict[str, Any]) -> str:
    """组合 subject、predicate、value、slot 与受控 tags 作为独立索引文本。"""
    tags = " ".join(str(tag) for tag in claim.get("topic_tags", []) if tag)
    return " ".join(
        str(value).strip()
        for value in (
            claim.get("subject_entity_id"),
            claim.get("predicate"),
            claim.get("value"),
            claim.get("canonical_slot"),
            tags,
        )
        if value is not None and str(value).strip()
    )
