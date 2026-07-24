from __future__ import annotations

import json
from typing import Any, Protocol

from hl_mem.config import DEDUP_SEMANTIC_THRESHOLD
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.entity import normalize_entity_id
from hl_mem.domain.claims.attributes import (
    canonical_conflict_slot,
    is_mutually_exclusive_attribute,
    normalize_canonical_attribute,
)


class ClaimRepositoryProtocol(Protocol):
    """声明去重所需的 Claim 查询能力。"""

    def find_active_for_dedup(self, namespace: str, subject_entity_id: str) -> list[dict[str, Any]]:
        """返回指定命名空间和主体的活跃 Claim。"""


DEDUP_COMPATIBLE_ATTRIBUTE_GROUPS = (
    frozenset({"choice.model", "config.model"}),
)


def _attributes_are_compatible(left: str | None, right: str | None) -> bool:
    left_normalized = normalize_canonical_attribute(left or "")
    right_normalized = normalize_canonical_attribute(right or "")
    if left_normalized == right_normalized:
        return True
    return any({left_normalized, right_normalized} <= group for group in DEDUP_COMPATIBLE_ATTRIBUTE_GROUPS)


class Deduplicator:
    def __init__(
        self, claim_repo: ClaimRepositoryProtocol, embedder: Any, threshold: float = DEDUP_SEMANTIC_THRESHOLD
    ) -> None:
        self.claim_repo, self.embedder, self.threshold = claim_repo, embedder, threshold

    def find_duplicate(self, new_claim: dict[str, Any]) -> tuple[str | None, str]:
        normalized_subject = normalize_entity_id(new_claim.get("subject_entity_id"))
        new_claim["subject_entity_id"] = normalized_subject
        candidates = self.claim_repo.find_active_for_dedup(
            new_claim.get("namespace_key", "default"), normalized_subject
        )
        value = self._canonical(new_claim.get("value_json"))
        for claim in candidates:
            same_conflict_key = new_claim.get("conflict_key") and claim.get("conflict_key") == new_claim.get(
                "conflict_key"
            )
            if same_conflict_key and self._canonical(claim.get("value_json")) == value:
                return claim["id"], "exact"
        blob = new_claim.get("embedding_dense")
        if blob is None:
            blob = self.embedder.embed_one(self._text(new_claim))
            new_claim["embedding_dense"] = blob
        best_claim: dict[str, Any] | None = None
        best_score = float("-inf")
        for claim in candidates:
            if not _attributes_are_compatible(
                claim.get("canonical_attribute"), new_claim.get("canonical_attribute")
            ):
                continue
            if self._values_are_mutually_exclusive(claim, new_claim):
                continue
            existing_blob = claim.get("embedding_dense")
            if existing_blob:
                score = cosine_similarity(existing_blob, blob)
                if score > best_score:
                    best_claim, best_score = claim, score
        if best_claim is not None and best_score >= self.threshold:
            return best_claim["id"], "semantic"
        return None, "new"

    @classmethod
    def _values_are_mutually_exclusive(cls, existing: dict[str, Any], new: dict[str, Any]) -> bool:
        existing_attribute = existing.get("canonical_attribute")
        new_attribute = new.get("canonical_attribute")
        values_differ = cls._canonical(existing.get("value_json")) != cls._canonical(new.get("value_json"))
        same_exclusive_slot = bool(
            is_mutually_exclusive_attribute(existing_attribute)
            and is_mutually_exclusive_attribute(new_attribute)
            and canonical_conflict_slot(existing_attribute) == canonical_conflict_slot(new_attribute)
        )
        cross_attribute_compatible = (
            normalize_canonical_attribute(existing_attribute or "")
            != normalize_canonical_attribute(new_attribute or "")
            and _attributes_are_compatible(existing_attribute, new_attribute)
        )
        return values_differ and (same_exclusive_slot or cross_attribute_compatible)

    @staticmethod
    def _canonical(value: Any) -> str:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _text(claim: dict[str, Any]) -> str:
        return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} {claim.get('value_json', '')}"
