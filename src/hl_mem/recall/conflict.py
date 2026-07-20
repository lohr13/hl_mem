from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any


EXCLUSIVE_QUALIFIERS = {"scope", "context", "environment", "project", "channel"}


def compute_conflict_key(
    namespace: str, subject: str, predicate: str, qualifiers: dict[str, Any] | None
) -> str:
    canonical_subject = re.sub(r"\s+", "", subject).casefold()
    exclusive = {key: value for key, value in (qualifiers or {}).items() if key in EXCLUSIVE_QUALIFIERS}
    raw = json.dumps(
        [namespace.casefold(), canonical_subject, predicate.casefold(), exclusive],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ConflictResolver:
    """First-version deterministic conflict classifier; it never calls an LLM."""

    def resolve(self, existing: dict[str, Any], new: dict[str, Any]) -> str:
        if existing.get("predicate") != new.get("predicate"):
            return "compatible"
        old_value, new_value = self._value(existing), self._value(new)
        if old_value == new_value:
            return "entails"
        if self._before(existing.get("valid_to"), new.get("valid_from")):
            return "state_change"
        if self._signals_change(new):
            return "state_change"
        if new.get("predicate") in {"preference", "service_status"}:
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
