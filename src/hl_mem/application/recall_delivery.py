"""Pure context-candidate and retrieval-bundle delivery transformations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from hl_mem.application.answerability import Answerability
from hl_mem.application.context_packet import (
    MemoryType,
    RetrievalBundle,
    RetrievalBundleItem,
    estimate_tokens,
    render_memory_text,
)
from hl_mem.domain.recall import RecallIntent
from hl_mem.recall.procedure_pipeline import MemoryCandidate

ContextText = Callable[[str, Mapping[str, Any]], str]
ContextPacker = Callable[[list[dict[str, Any]], int], list[dict[str, Any]]]


def budget_pack_by_type(
    candidates: list[MemoryCandidate],
    intent: RecallIntent,
    token_budget: int,
) -> tuple[list[MemoryCandidate], dict[str, int], int]:
    """Pack Tool/Procedure candidates by type quota, then reflow unused budget."""
    ratios = (
        {"policy": 0.35, "episode": 0.25, "trace": 0.15, "claim": 0.25}
        if intent is RecallIntent.TOOL
        else {"policy": 0.40, "episode": 0.20, "trace": 0.25, "claim": 0.15}
    )
    quotas = {kind: int(token_budget * ratio) for kind, ratio in ratios.items()}
    grouped = {kind: [item for item in candidates if item.memory_type == kind] for kind in ratios}
    packed: list[MemoryCandidate] = []
    used_by_type = {kind: 0 for kind in ratios}
    remaining = {kind: list(items) for kind, items in grouped.items()}

    def candidate_text(item: MemoryCandidate) -> str:
        return (
            render_memory_text(
                item.text,
                role=item.role,
                action=item.action,
                object_=item.object,
            )
            if item.memory_type == "claim"
            else item.text
        )

    def take(kind: str, allowance: int) -> int:
        used = 0
        retained: list[MemoryCandidate] = []
        for item in remaining[kind]:
            cost = estimate_tokens(candidate_text(item))
            if used + cost <= allowance:
                packed.append(item)
                used += cost
            else:
                retained.append(item)
        remaining[kind] = retained
        used_by_type[kind] += used
        return used

    for kind, quota in quotas.items():
        take(kind, quota)
    total_used = sum(used_by_type.values())
    reflow_budget = max(0, token_budget - total_used)
    reflow_used = 0
    for kind in ("policy", "episode", "claim", "trace"):
        used = take(kind, reflow_budget)
        reflow_used += used
        reflow_budget -= used
    order = {id(item): index for index, item in enumerate(candidates)}
    packed.sort(key=lambda item: order[id(item)])
    return packed, quotas, reflow_used


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
