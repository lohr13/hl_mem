from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

from hl_mem.domain.claims.attributes import (
    canonical_conflict_slot,
    is_mutually_exclusive_attribute,
    normalize_canonical_attribute,
    normalize_predicate,
)


EXCLUSIVE_QUALIFIERS = {"scope", "context", "environment", "project", "channel"}


def compute_claim_pair_key(left_claim_id: str, right_claim_id: str) -> str:
    """按 claim ID 无序计算稳定的冲突对标识。"""
    claim_ids = sorted((left_claim_id, right_claim_id))
    return hashlib.sha256("\0".join(claim_ids).encode()).hexdigest()[:24]


def compute_conflict_key(
    namespace: str,
    subject: str,
    canonical_attribute: str,
    qualifiers: dict[str, Any] | None,
    *,
    version: int = 2,
) -> str:
    """计算 canonical attribute v2 冲突键。"""
    if version != 2:
        raise ValueError("compute_conflict_key only supports version 2")
    canonical_namespace = unicodedata.normalize("NFKC", namespace).strip().casefold()
    canonical_subject = re.sub(r"\s+", "", unicodedata.normalize("NFKC", subject)).casefold()
    exclusive = {
        key: _canonicalize_json(value)
        for key, value in (qualifiers or {}).items()
        if key in EXCLUSIVE_QUALIFIERS
    }
    slot = canonical_conflict_slot(normalize_canonical_attribute(canonical_attribute))
    raw = json.dumps(
        ["v2", canonical_namespace, canonical_subject, slot, exclusive],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_legacy_conflict_key(
    namespace: str,
    subject: str,
    predicate: str,
    qualifiers: dict[str, Any] | None,
) -> str:
    """复现 v1 算法，供迁移期审计和回滚使用。"""
    canonical_subject = re.sub(r"\s+", "", subject).casefold()
    exclusive = {key: value for key, value in (qualifiers or {}).items() if key in EXCLUSIVE_QUALIFIERS}
    raw = json.dumps(
        [namespace.casefold(), canonical_subject, predicate.casefold(), exclusive],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _canonicalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value).strip().casefold()
    if isinstance(value, dict):
        return {str(key): _canonicalize_json(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize_json(item) for item in value]
    return value


class ConflictResolver:
    """First-version deterministic conflict classifier; it never calls an LLM."""

    def resolve(self, existing: dict[str, Any], new: dict[str, Any]) -> str:
        existing_attribute = existing.get("canonical_attribute")
        new_attribute = new.get("canonical_attribute")
        if not (
            is_mutually_exclusive_attribute(existing_attribute)
            and is_mutually_exclusive_attribute(new_attribute)
        ):
            return "compatible"
        if canonical_conflict_slot(existing_attribute) != canonical_conflict_slot(new_attribute):
            return "compatible"
        old_value, new_value = self._value(existing), self._value(new)
        if old_value == new_value:
            return "entails"
        if self._before(existing.get("valid_to"), new.get("valid_from")):
            return "state_change"
        if self._signals_change(new):
            return "state_change"
        new_predicate = normalize_predicate(str(new.get("predicate", "")))
        if new_predicate in {"偏好", "状态"}:
            return "state_change"
        if existing.get("source_authority", "medium") == new.get("source_authority", "medium"):
            return "contradicts"
        return "uncertain"

    @staticmethod
    def _value(claim: dict[str, Any]) -> Any:
        value = claim.get("value", claim.get("value_json"))
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _before(old_to: str | None, new_from: str | None) -> bool:
        if not old_to or not new_from:
            return False
        try:
            return datetime.fromisoformat(old_to) <= datetime.fromisoformat(new_from)
        except ValueError:
            return old_to <= new_from

    @staticmethod
    def _signals_change(claim: dict[str, Any]) -> bool:
        qualifiers = claim.get("qualifiers") or claim.get("qualifiers_json") or {}
        if isinstance(qualifiers, str):
            try:
                qualifiers = json.loads(qualifiers)
            except json.JSONDecodeError:
                qualifiers = {}
        return bool(
            qualifiers.get("state_change") or qualifiers.get("current") or qualifiers.get("change")
        )
