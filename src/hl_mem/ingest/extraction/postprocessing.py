"""Deterministic projection and merge operations for extracted claims."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from typing import Any, Mapping, cast

from hl_mem.domain.action_coordinates import project_action_qualifiers
from hl_mem.domain.claims.attributes import (
    infer_canonical_attribute,
    normalize_predicate,
    normalize_topic_tags,
    predicate_for_canonical_attribute,
    reconcile_canonical_attribute,
    validate_slot_instance,
)
from hl_mem.domain.entity import (
    invalid_subject_reason,
    isolated_subject_id,
    normalize_entity_id,
)
from hl_mem.observability.audit import current_audit

from ..extractors import AssertionKind, ExtractedClaim


def merge_chunk_claims(chunks: list[list[ExtractedClaim]]) -> list[ExtractedClaim]:
    """Stably merge duplicate claims emitted by chunks in the same extraction."""
    merged: list[ExtractedClaim] = []
    positions: dict[tuple[str, str, str, str, str], int] = {}
    for claims in chunks:
        for claim in claims:
            key = (
                unicodedata.normalize("NFKC", claim.subject).strip().casefold(),
                unicodedata.normalize("NFKC", claim.predicate).strip().casefold(),
                unicodedata.normalize("NFKC", claim.canonical_slot or "").strip().casefold(),
                unicodedata.normalize("NFKC", str(claim.value)).strip().casefold(),
                unicodedata.normalize(
                    "NFKC",
                    json.dumps(
                        claim.qualifiers,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                ),
            )
            if key in positions:
                position = positions[key]
                existing = merged[position]
                indices = tuple(dict.fromkeys((*existing.source_event_indices, *claim.source_event_indices)))
                merged[position] = replace(existing, source_event_indices=indices)
                continue
            positions[key] = len(merged)
            merged.append(claim)
    return merged


def claim_from_payload(
    item: dict[str, Any],
    *,
    preserve_subject: bool = False,
    aliases: Mapping[str, str],
) -> ExtractedClaim:
    """Project one compatible wire claim into the internal claim value object."""
    value = str(item.get("value", "")).strip()
    value = aliases.get(value.casefold(), value)
    predicate = normalize_predicate(str(item.get("predicate", "事实")).strip())
    original_subject = str(item.get("subject", "用户"))
    subject = (
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", original_subject).strip())
        if preserve_subject
        else normalize_entity_id(original_subject)
    )
    entities = list(item.get("entities") or [])
    invalid_reason = invalid_subject_reason(original_subject)
    if invalid_reason is not None:
        replacement = next(
            (normalize_entity_id(entity) for entity in entities if invalid_subject_reason(entity) is None),
            None,
        )
        subject = replacement or isolated_subject_id(original_subject, predicate, value)
        if original_subject not in entities:
            entities.append(original_subject)
        current_audit().emit(
            "extract",
            "subject_guard",
            "replaced" if replacement else "isolated",
            detail={
                "original_subject": original_subject,
                "normalized_subject": normalize_entity_id(original_subject),
                "replacement_subject": subject,
                "reason_code": invalid_reason,
                "isolation_reason": None if replacement else "invalid_subject_isolated",
            },
        )
    qualifiers = item.get("qualifiers") or {}
    inferred_attribute = infer_canonical_attribute(predicate, subject, value, qualifiers)
    canonical_attribute, _attribute_reason = reconcile_canonical_attribute(
        predicate=predicate,
        llm_attribute=str(item.get("canonical_attribute", "")),
        inferred_attribute=inferred_attribute,
        subject=subject,
        value=value,
        qualifiers=qualifiers,
    )
    projected_predicate = predicate_for_canonical_attribute(canonical_attribute, predicate)
    current_audit().emit(
        "extract",
        "predicate_normalized",
        "changed" if projected_predicate != predicate else "preserved",
        detail={
            "llm_predicate": predicate,
            "normalized_predicate": projected_predicate,
            "canonical_attribute": canonical_attribute,
            "reason_code": "canonical_attribute_projection" if projected_predicate != predicate else "llm_preserved",
        },
    )
    predicate = projected_predicate
    qualifiers = project_action_qualifiers(
        value,
        qualifiers,
        is_plan=canonical_attribute.startswith("plan.") or predicate == "计划",
    )
    volatility = item.get("volatility", "stable")
    scope = item.get("scope", "permanent")
    scope = scope if scope in {"temporal", "permanent"} else "permanent"
    try:
        confidence = min(1.0, max(0.0, float(item.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    try:
        importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))
    except (TypeError, ValueError):
        importance = 0.5
    return ExtractedClaim(
        predicate=predicate,
        value=value,
        confidence=confidence,
        volatility=volatility if volatility in {"stable", "ephemeral"} else "stable",
        subject=subject,
        qualifiers=qualifiers,
        reason=str(item.get("reason", "")),
        scope=scope,
        importance=importance,
        canonical_attribute=canonical_attribute,
        canonical_slot=validate_slot_instance(item.get("canonical_slot"), qualifiers),
        topic_tags=normalize_topic_tags(item.get("topic_tags")),
        occurred_start=item.get("occurred_start"),
        occurred_end=item.get("occurred_end"),
        entities=entities or None,
        memory_layer="episodic" if item.get("memory_layer") == "episodic" else "durable",
        assertion_kind=(
            cast(AssertionKind, item.get("assertion_kind"))
            if item.get("assertion_kind") in {"unknown", "observation", "inference"}
            else "unknown"
        ),
        source_event_indices=tuple(item.get("source_event_indices") or ()),
    )
