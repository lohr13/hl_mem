"""Hermes Context Packet 的确定性纯函数 renderer。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hl_mem.application.context_packet import render_memory_text


@dataclass(frozen=True, slots=True)
class RenderedContext:
    """渲染文本及其中实际包含的 feedback receipt。"""

    text: str
    included_feedback_ids: tuple[str, ...]


def render_context(payload: Mapping[str, Any]) -> RenderedContext:
    """按 Context Packet v1 的 item 顺序渲染非空文本。

    renderer 只消费 ``items[].text``，并单独返回对应的
    ``feedback_id``；其他 packet 或 item 字段不会进入提示词。
    """

    schema_major = payload.get("schema_major")
    if not isinstance(schema_major, int) or isinstance(schema_major, bool) or schema_major != 1:
        raise ValueError(f"unsupported context packet schema major: {schema_major!r}")

    raw_items = payload.get("items")
    if not isinstance(raw_items, (list, tuple)):
        return RenderedContext("", ())

    texts: list[str] = []
    feedback_ids: list[str] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        texts.append(
            render_memory_text(
                text,
                role=item.get("role") if item.get("type") == "claim" else None,
                action=item.get("action") if item.get("type") == "claim" else None,
                object_=item.get("object") if item.get("type") == "claim" else None,
            )
        )
        feedback_id = item.get("feedback_id")
        if isinstance(feedback_id, str) and feedback_id:
            feedback_ids.append(feedback_id)

    return RenderedContext("\n".join(texts), tuple(feedback_ids))


__all__ = ["RenderedContext", "render_context"]
