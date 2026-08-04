"""Claim 精确、语义及跨主体去重领域逻辑。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from hl_mem.config import DEDUP_SEMANTIC_THRESHOLD
from hl_mem.core.vector import cosine_similarity
from hl_mem.domain.claims.attributes import (
    canonical_conflict_slot,
    is_mutually_exclusive_attribute,
    normalize_predicate,
)
from hl_mem.domain.claims.conflicts import slot_qualifier_key
from hl_mem.domain.entity import normalize_entity_id

DEDUP_EMBEDDING_TEXT_VERSION = "v1: predicate+value"
DEDUP_POLICY_VERSION = "v2"


def compute_dedup_pair_key(left_claim_id: str, right_claim_id: str) -> str:
    """Return the stable, order-independent key used by ``dedup_pairs``."""
    ordered = "\x1f".join(sorted((left_claim_id, right_claim_id)))
    return hashlib.sha256(ordered.encode("utf-8")).hexdigest()


class ClaimRepositoryProtocol(Protocol):
    """声明去重所需的 Claim 查询能力。"""

    def find_active_for_dedup(
        self,
        namespace: str,
        subject_entity_id: str,
        canonical_slot: str,
        qualifier_key: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """返回指定 slot、qualifier 和主体的活跃 Claim。"""

    def find_cross_predicate_candidates(
        self,
        namespace: str,
        subject_entity_id: str,
        predicate: str,
    ) -> list[dict[str, Any]]:
        """返回无 slot 且 predicate、主体相同的活跃 Claim。"""


class Deduplicator:
    def __init__(
        self,
        claim_repo: ClaimRepositoryProtocol,
        embedder: Any,
        threshold: float = DEDUP_SEMANTIC_THRESHOLD,
    ) -> None:
        self.claim_repo, self.embedder, self.threshold = claim_repo, embedder, threshold

    def find_duplicate(self, new_claim: dict[str, Any]) -> tuple[str | None, str]:
        normalized_subject = normalize_entity_id(new_claim.get("subject_entity_id"))
        new_claim["subject_entity_id"] = normalized_subject
        namespace = new_claim.get("namespace_key", "default")
        canonical_slot = new_claim.get("canonical_slot")
        if canonical_slot:
            candidates = self.claim_repo.find_active_for_dedup(
                namespace,
                normalized_subject,
                canonical_slot,
                slot_qualifier_key(canonical_slot, new_claim.get("qualifiers")),
            )
        else:
            candidates = self.claim_repo.find_cross_predicate_candidates(
                namespace,
                normalized_subject,
                str(new_claim.get("predicate", "")),
            )
        gray_candidates: list[dict[str, Any]] = []
        for claim in candidates:
            deterministic = self._deterministic_check(claim, new_claim)
            if deterministic == "equivalent":
                return claim["id"], "exact"
            if deterministic == "distinct" or self._values_are_mutually_exclusive(claim, new_claim):
                continue
            gray_candidates.append(claim)
        if not gray_candidates:
            return None, "new"
        blob = new_claim.get("embedding_dense")
        if blob is None:
            blob = self.embedder.embed_one(self._text(new_claim))
            new_claim["embedding_dense"] = blob
        best_claim: dict[str, Any] | None = None
        best_score = float("-inf")
        for claim in gray_candidates:
            existing_blob = claim.get("embedding_dense")
            if existing_blob:
                score = cosine_similarity(existing_blob, blob)
                if score > best_score:
                    best_claim, best_score = claim, score
        if best_claim is not None and best_score >= self.threshold:
            return best_claim["id"], "semantic_candidate"
        return None, "new"

    @classmethod
    def _deterministic_check(cls, existing: dict[str, Any], new: dict[str, Any]) -> str | None:
        """Return ``equivalent``, ``distinct``, or ``None`` for an LLM gray area."""
        if str(existing.get("namespace_key", "default")) != str(new.get("namespace_key", "default")):
            return "distinct"
        if normalize_entity_id(existing.get("subject_entity_id")) != normalize_entity_id(new.get("subject_entity_id")):
            return "distinct"

        existing_slot = existing.get("canonical_slot")
        new_slot = new.get("canonical_slot")
        if existing_slot != new_slot and (existing_slot or new_slot):
            return "distinct"

        existing_attribute = existing.get("canonical_attribute")
        new_attribute = new.get("canonical_attribute")
        if existing_attribute and new_attribute and existing_attribute != new_attribute:
            return "distinct"

        if not existing_slot and not new_slot:
            if normalize_predicate(str(existing.get("predicate", ""))) != normalize_predicate(
                str(new.get("predicate", ""))
            ):
                return "distinct"

        existing_qualifiers = existing.get("qualifiers") or {}
        new_qualifiers = new.get("qualifiers") or {}
        if not isinstance(existing_qualifiers, dict) or not isinstance(new_qualifiers, dict):
            return None
        for key in existing_qualifiers.keys() & new_qualifiers.keys():
            if existing_qualifiers[key] != new_qualifiers[key]:
                return "distinct"

        if (
            existing_attribute == new_attribute
            and existing_qualifiers == new_qualifiers
            and cls._canonical_claim(existing) == cls._canonical_claim(new)
        ):
            return "equivalent"
        return None

    @classmethod
    def _values_are_mutually_exclusive(cls, existing: dict[str, Any], new: dict[str, Any]) -> bool:
        existing_slot = existing.get("canonical_slot")
        new_slot = new.get("canonical_slot")
        values_differ = cls._canonical_claim(existing) != cls._canonical_claim(new)
        same_exclusive_slot = bool(
            isinstance(existing_slot, str)
            and isinstance(new_slot, str)
            and is_mutually_exclusive_attribute(existing_slot)
            and is_mutually_exclusive_attribute(new_slot)
            and canonical_conflict_slot(existing_slot) == canonical_conflict_slot(new_slot)
        )
        return values_differ and same_exclusive_slot

    @classmethod
    def _canonical_claim(cls, claim: dict[str, Any]) -> str:
        """规范化声明值，避免对仓储已解码的字符串再次 JSON 解码。"""
        return json.dumps(
            claim.get("value"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _text(claim: dict[str, Any]) -> str:
        return f"{claim.get('subject_entity_id', '')} {claim.get('predicate', '')} " f"{claim.get('value', '')}"
