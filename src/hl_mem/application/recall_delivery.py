"""Pure context-candidate and retrieval-bundle delivery transformations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from hl_mem.application.answerability import Answerability
from hl_mem.application.context_packet import MemoryType, RetrievalBundle, RetrievalBundleItem, estimate_tokens

ContextText = Callable[[str, Mapping[str, Any]], str]
ContextPacker = Callable[[list[dict[str, Any]], int], list[dict[str, Any]]]


def context_candidates(
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return untrimmed context candidates in stable type priority."""
    all_items: list[dict[str, Any]] = (
        [{"type": "claim", "data": item, "priority": 2} for item in claims]
        + [{"type": "observation", "data": item, "priority": 1} for item in observations]
        + [{"type": "policy", "data": item, "priority": 0} for item in policies]
    )
    all_items.sort(key=lambda item: -item["priority"] if isinstance(item.get("priority"), int) else 0)
    return all_items


def assemble_context(
    claims: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    policies: list[dict[str, Any]],
    token_budget: int,
    *,
    packer: ContextPacker,
    text_for: ContextText,
) -> dict[str, Any]:
    """Pack cross-type context within the caller's token budget."""
    all_items = context_candidates(claims, observations, policies)
    packed = packer(all_items, token_budget)
    used = 0
    for item in packed:
        data = item.get("data", item)
        memory_type = str(item.get("type") or data.get("memory_type") or data.get("type") or "")
        used += estimate_tokens(text_for(memory_type, data))
    return {
        "context_items": packed,
        "used_tokens_estimate": used,
        "truncated": len(packed) < len(all_items),
    }


def bundle_from_context_items(
    query_id: str,
    answerability: Answerability,
    context_items: list[dict[str, Any]],
    *,
    text_for: ContextText,
) -> RetrievalBundle:
    """Project ordered candidates into a cacheable bundle without receipts."""
    items: list[RetrievalBundleItem] = []
    for wrapped in context_items:
        data = wrapped.get("data", wrapped)
        memory_type = str(wrapped.get("type") or data.get("memory_type") or data.get("type") or "")
        raw_evidence = data.get("evidence") or []
        evidence = tuple(reference for reference in raw_evidence if isinstance(reference, Mapping))
        raw_score = data.get("_score", data.get("score"))
        try:
            score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            score = None
        items.append(
            RetrievalBundleItem(
                cast(MemoryType, memory_type),
                str(data["id"]),
                str(data.get("text") or "") if memory_type == "claim" else text_for(memory_type, data),
                evidence,
                score,
                str(data["role"]) if memory_type == "claim" and data.get("role") else None,
                str(data["action"]) if memory_type == "claim" and data.get("action") else None,
                str(data["object"]) if memory_type == "claim" and data.get("object") else None,
            )
        )
    return RetrievalBundle(query_id=query_id, answerability=answerability, items=tuple(items))


def context_from_packed_bundle(
    context_items: list[dict[str, Any]],
    bundle: RetrievalBundle,
) -> dict[str, Any]:
    """Project a decorated packed bundle back to the legacy context shape."""
    remaining = list(context_items)
    selected: list[dict[str, Any]] = []
    for bundle_item in bundle.items:
        match_index = next(
            (
                index
                for index, wrapped in enumerate(remaining)
                if str(wrapped.get("type") or wrapped.get("data", wrapped).get("memory_type") or "") == bundle_item.type
                and str(wrapped.get("data", wrapped).get("id") or "") == bundle_item.id
            ),
            None,
        )
        if match_index is None:
            continue
        wrapped = remaining.pop(match_index)
        data = dict(wrapped.get("data", wrapped))
        if bundle_item.type == "claim":
            data["text"] = bundle_item.text
        selected.append({**wrapped, "data": data} if "data" in wrapped else data)
    return {
        "context_items": selected,
        "used_tokens_estimate": int(bundle.used_tokens_estimate or 0),
        "truncated": bool(bundle.truncated),
    }
