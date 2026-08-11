"""Claim 精确、语义及跨主体去重领域逻辑。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
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
DETERMINISTIC_NEAR_COPY_REASON = "deterministic_near_copy_v1"
NEAR_COPY_LEXICAL_THRESHOLD = 0.90

_PROTECTED_LITERAL_PATTERN = re.compile(
    r"(?<!\w)(?:v?\d+(?:\.\d+)+|\d+)(?!\w)|(?:[A-Za-z]:\\|/)[^\s\"']+",
    re.IGNORECASE,
)
_QUOTED_VALUE_PATTERN = re.compile(r'"([^"\r\n]+)"|“([^”\r\n]+)”|(?<!\w)\'([^\'\r\n]+)\'(?!\w)')
_WORD_PATTERN = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)
_PROPER_NAME_PATTERN = re.compile(r"(?<![\w])(?:[A-Z][A-Za-z0-9_-]{2,})(?![\w])")
_CJK_PROTECTED_PATTERN = re.compile(
    r"大前天|前天|昨天|今天|明天|后天|早晨|早上|上午|中午|下午|傍晚|晚上|凌晨|"
    r"没有|禁止|不|没|未|无|(?:星期|周)[一二三四五六日天]"
)
_PROTECTED_WORDS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "today",
    "tomorrow",
    "yesterday",
    "morning",
    "afternoon",
    "evening",
    "night",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "not",
    "no",
    "never",
    "without",
    "cannot",
    "can't",
    "couldn't",
    "didn't",
    "doesn't",
    "don't",
    "false",
    "hadn't",
    "hasn't",
    "haven't",
    "isn't",
    "true",
    "shouldn't",
    "wasn't",
    "weren't",
    "won't",
    "wouldn't",
}
_GENERIC_CAPITALIZED_WORDS = {"a", "an", "i", "the", "this", "that", "user"}


def compute_dedup_pair_key(left_claim_id: str, right_claim_id: str) -> str:
    """Return the stable, order-independent key used by ``dedup_pairs``."""
    ordered = "\x1f".join(sorted((left_claim_id, right_claim_id)))
    return hashlib.sha256(ordered.encode("utf-8")).hexdigest()


def _near_copy_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = _WORD_PATTERN.findall(normalized)
    if words[:1] == ["the"]:
        words = words[1:]
    return " ".join(words)


def _protected_atoms(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    positioned_atoms: list[tuple[int, int, str]] = []
    for match in _PROTECTED_LITERAL_PATTERN.finditer(normalized):
        positioned_atoms.append((match.start(), match.end(), f"literal:{match.group(0).casefold()}"))
    for match in _QUOTED_VALUE_PATTERN.finditer(normalized):
        quoted = next((group for group in match.groups() if group is not None), "").strip().casefold()
        if quoted:
            positioned_atoms.append((match.start(), match.end(), f"quoted:{quoted}"))
    for match in _WORD_PATTERN.finditer(normalized):
        word = match.group(0).casefold()
        if word in _PROTECTED_WORDS:
            positioned_atoms.append((match.start(), match.end(), f"word:{word}"))
    for match in _CJK_PROTECTED_PATTERN.finditer(normalized):
        positioned_atoms.append((match.start(), match.end(), f"cjk:{match.group(0)}"))
    for match in _PROPER_NAME_PATTERN.finditer(normalized):
        name = match.group(0).casefold()
        if name not in _GENERIC_CAPITALIZED_WORDS and name not in _PROTECTED_WORDS:
            positioned_atoms.append((match.start(), match.end(), f"name:{name}"))
    positioned_atoms.sort(key=lambda atom: (atom[0], atom[1], atom[2]))
    return tuple(atom[2] for atom in positioned_atoms)


def _verified_user_projection(
    left_subject: str,
    right_subject: str,
    left_value: str,
    right_value: str,
) -> tuple[str, str] | None:
    if left_subject == "user" and right_subject.startswith("user's "):
        user_subject, projected_subject = left_subject, right_subject
    elif right_subject == "user" and left_subject.startswith("user's "):
        user_subject, projected_subject = right_subject, left_subject
    else:
        return None
    projected_suffix = projected_subject.removeprefix("user's ").strip()
    suffix_text = _near_copy_text(projected_suffix)
    left_text = _near_copy_text(left_value)
    right_text = _near_copy_text(right_value)
    if suffix_text is None or left_text is None or right_text is None:
        return None
    suffix_tokens = suffix_text.split()
    if not suffix_tokens:
        return None
    left_tokens = set(left_text.split())
    right_tokens = set(right_text.split())
    if not all(token in left_tokens and token in right_tokens for token in suffix_tokens):
        return None
    return user_subject, projected_subject


def _normalized_entities(claim: dict[str, Any]) -> tuple[str, ...] | None:
    raw_entities = claim.get("entities")
    if raw_entities in (None, []):
        return ()
    if not isinstance(raw_entities, (list, tuple)) or not all(isinstance(entity, str) for entity in raw_entities):
        return None
    return tuple(sorted({normalize_entity_id(entity) for entity in raw_entities}))


def _entity_mention_signature(value: str, entities: tuple[str, ...]) -> tuple[str, ...]:
    normalized_value = unicodedata.normalize("NFKC", value).casefold()
    mentions: set[tuple[int, int, str]] = set()
    for entity in entities:
        normalized_entity = unicodedata.normalize("NFKC", entity).casefold().strip()
        if not normalized_entity:
            continue
        escaped = re.escape(normalized_entity)
        if any("a" <= character <= "z" or character.isdigit() for character in normalized_entity):
            pattern = re.compile(rf"(?<!\w){escaped}(?!\w)")
        else:
            pattern = re.compile(escaped)
        for match in pattern.finditer(normalized_value):
            mentions.add((match.start(), match.end(), entity))
    return tuple(mention[2] for mention in sorted(mentions))


def _entities_compatible(
    left: dict[str, Any],
    right: dict[str, Any],
    projection: tuple[str, str] | None,
    left_value: str,
    right_value: str,
) -> bool:
    left_entities = _normalized_entities(left)
    right_entities = _normalized_entities(right)
    if left_entities is None or right_entities is None:
        return False
    if left_entities == right_entities:
        return _entity_mention_signature(left_value, left_entities) == _entity_mention_signature(
            right_value,
            right_entities,
        )
    if projection is None or not left_entities or not right_entities:
        return False
    allowed_entities = set(projection)
    return (
        set(left_entities).issubset(allowed_entities)
        and set(right_entities).issubset(allowed_entities)
        and normalize_entity_id(left.get("subject_entity_id")) in left_entities
        and normalize_entity_id(right.get("subject_entity_id")) in right_entities
    )


def _valid_intervals_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def parse(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    try:
        left_start, left_end = parse(left.get("valid_from")), parse(left.get("valid_to"))
        right_start, right_end = parse(right.get("valid_from")), parse(right.get("valid_to"))
    except (TypeError, ValueError):
        return False
    return (left_end is None or right_start is None or right_start < left_end) and (
        right_end is None or left_start is None or left_start < right_end
    )


def is_safe_near_duplicate(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    similarity: float,
    semantic_threshold: float,
    allow_subject_mismatch: bool = False,
) -> bool:
    """Return true only when cheap signals jointly prove a conservative near-copy."""
    if similarity < semantic_threshold:
        return False
    if left.get("status", "active") == "disputed" or right.get("status", "active") == "disputed":
        return False
    if str(left.get("namespace_key", "default")) != str(right.get("namespace_key", "default")):
        return False
    left_value = left.get("value")
    right_value = right.get("value")
    if not isinstance(left_value, str) or not isinstance(right_value, str):
        return False
    left_subject = normalize_entity_id(left.get("subject_entity_id"))
    right_subject = normalize_entity_id(right.get("subject_entity_id"))
    projection: tuple[str, str] | None = None
    if left_subject != right_subject:
        if not allow_subject_mismatch:
            return False
        projection = _verified_user_projection(left_subject, right_subject, left_value, right_value)
        if projection is None:
            return False
    if not _entities_compatible(left, right, projection, left_value, right_value):
        return False
    if normalize_predicate(str(left.get("predicate") or "")) != normalize_predicate(str(right.get("predicate") or "")):
        return False
    if left.get("canonical_slot") != right.get("canonical_slot"):
        return False
    if left.get("canonical_attribute") != right.get("canonical_attribute"):
        return False
    left_qualifiers = left.get("qualifiers") or {}
    right_qualifiers = right.get("qualifiers") or {}
    if not isinstance(left_qualifiers, dict) or not isinstance(right_qualifiers, dict):
        return False
    if left_qualifiers != right_qualifiers or not _valid_intervals_overlap(left, right):
        return False
    left_text = _near_copy_text(left_value)
    right_text = _near_copy_text(right_value)
    if left_text is None or right_text is None:
        return False
    if _protected_atoms(left_value) != _protected_atoms(right_value):
        return False
    return SequenceMatcher(None, left_text, right_text, autojunk=False).ratio() >= NEAR_COPY_LEXICAL_THRESHOLD


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
        scored_candidates: list[tuple[dict[str, Any], float]] = []
        for claim in gray_candidates:
            existing_blob = claim.get("embedding_dense")
            if existing_blob:
                score = cosine_similarity(existing_blob, blob)
                scored_candidates.append((claim, score))
        scored_candidates.sort(key=lambda item: (-item[1], str(item[0].get("id") or "")))
        for claim, score in scored_candidates:
            if is_safe_near_duplicate(
                claim,
                new_claim,
                similarity=score,
                semantic_threshold=self.threshold,
            ):
                return claim["id"], "near_duplicate"
        best_claim, best_score = scored_candidates[0] if scored_candidates else (None, float("-inf"))
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
