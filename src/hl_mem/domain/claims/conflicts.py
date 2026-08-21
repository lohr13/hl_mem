"""Claim 冲突键、qualifier 规范化与确定性冲突判定。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any

from hl_mem.domain.claims.attributes import (
    SLOT_REGISTRY,
    canonical_conflict_slot,
    is_mutually_exclusive_attribute,
    normalize_predicate,
    validate_slot_instance,
)
from hl_mem.domain.constants import PREDICATE_PREFERENCE, PREDICATE_STATE

EXCLUSIVE_QUALIFIERS = {"scope", "context", "environment", "project", "channel"}

_CONFLICT_FINGERPRINT_CLAIM_FIELDS = (
    "id",
    "status",
    "source_authority",
    "namespace_key",
    "canonical_slot",
    "conflict_key",
    "superseded_by_id",
    "value_json",
    "qualifiers_json",
    "valid_from",
    "valid_to",
)


def compute_claim_pair_key(left_claim_id: str, right_claim_id: str) -> str:
    """按 claim ID 无序计算稳定的冲突对标识。"""
    claim_ids = sorted((left_claim_id, right_claim_id))
    return hashlib.sha256("\0".join(claim_ids).encode()).hexdigest()[:24]


def compute_conflict_group_key(namespace: str, conflict_key: str) -> str:
    """返回由 namespace 分区约束的稳定组键。"""

    if not namespace.strip() or not conflict_key.strip():
        raise ValueError("conflict group namespace and key must not be empty")
    return conflict_key


def compute_conflict_group_case_key(namespace: str, group_key: str, generation: int) -> str:
    """为兼容 pair_key 唯一列生成不依赖代表端点的组案标识。"""

    raw = json.dumps(
        ["group", namespace, group_key, generation],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def conflict_review_fingerprint(
    case: dict[str, Any],
    left_tip: dict[str, Any],
    right_tip: dict[str, Any],
) -> str:
    """散列一次自动裁决实际读取的稳定输入快照。"""

    payload = {
        "case": {
            "id": case.get("id"),
            "status": case.get("status"),
            "generation": case.get("generation", 1),
            "revision": case.get("revision", 0),
            "namespace_key": case.get("namespace_key"),
            "group_key": case.get("group_key"),
            "overflow": case.get("overflow", 0),
        },
        "left_tip": {field: left_tip.get(field) for field in _CONFLICT_FINGERPRINT_CLAIM_FIELDS},
        "right_tip": {field: right_tip.get(field) for field in _CONFLICT_FINGERPRINT_CLAIM_FIELDS},
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def conflict_group_review_fingerprint(
    case: dict[str, Any],
    members: list[dict[str, Any]],
) -> str:
    """散列 group-native 裁决实际读取的完整成员快照。"""

    payload = {
        "case": {
            "id": case.get("id"),
            "status": case.get("status"),
            "generation": case.get("generation", 1),
            "revision": case.get("revision", 0),
            "namespace_key": case.get("namespace_key"),
            "group_key": case.get("group_key"),
            "overflow": case.get("overflow", 0),
        },
        "members": [
            {
                "candidate_key": member.get("candidate_key"),
                **{field: member.get(field) for field in _CONFLICT_FINGERPRINT_CLAIM_FIELDS},
            }
            for member in sorted(
                members,
                key=lambda item: (str(item.get("candidate_key") or ""), str(item.get("id") or "")),
            )
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_conflict_key(
    namespace: str,
    subject: str,
    predicate: str,
    canonical_slot: str | None,
    qualifiers: dict[str, Any] | None,
    *,
    version: int = 3,
) -> str | None:
    """按 operational slot instance 计算 v3 冲突键；无有效 slot 时不生成键。"""
    if version != 3:
        raise ValueError("compute_conflict_key only supports version 3")
    slot = validate_slot_instance(canonical_slot, qualifiers)
    if slot is None:
        return None
    canonical_namespace = unicodedata.normalize("NFKC", namespace).strip().casefold()
    canonical_subject = re.sub(r"\s+", "", unicodedata.normalize("NFKC", subject)).casefold()
    del predicate  # v3 由 slot 唯一决定冲突语义，predicate 不再隔离同一事实。
    raw = json.dumps(
        [
            "v3",
            canonical_namespace,
            canonical_subject,
            slot,
            coordinate_qualifier_key(slot, qualifiers),
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def coordinate_qualifier_key(canonical_slot: str | None, qualifiers: dict[str, Any] | None) -> dict[str, Any]:
    """提取并规范化 slot 声明的坐标 qualifier。"""
    slot = validate_slot_instance(canonical_slot, qualifiers)
    if slot is None:
        return {}
    values = qualifiers or {}
    return {key: _canonicalize_json(values.get(key)) for key in SLOT_REGISTRY[slot].coordinate_qualifiers}


def slot_qualifier_key(canonical_slot: str | None, qualifiers: dict[str, Any] | None) -> dict[str, Any]:
    """兼容旧调用面，返回 slot 的坐标 qualifier 投影。"""

    return coordinate_qualifier_key(canonical_slot, qualifiers)


class ConflictResolver:
    """First-version deterministic conflict classifier; it never calls an LLM."""

    def resolve(self, existing: dict[str, Any], new: dict[str, Any]) -> str:
        existing_slot = existing.get("canonical_slot")
        new_slot = new.get("canonical_slot")
        if not (
            isinstance(existing_slot, str)
            and isinstance(new_slot, str)
            and is_mutually_exclusive_attribute(existing_slot)
            and is_mutually_exclusive_attribute(new_slot)
        ):
            return "compatible"
        if canonical_conflict_slot(existing_slot) != canonical_conflict_slot(new_slot):
            return "compatible"
        old_value, new_value = self._value(existing), self._value(new)
        if old_value == new_value:
            return "entails"
        if self._before(existing.get("valid_to"), new.get("valid_from")):
            return "state_change"
        if self._signals_change(new):
            return "state_change"
        new_predicate = normalize_predicate(str(new.get("predicate", "")))
        if new_predicate in {PREDICATE_PREFERENCE, PREDICATE_STATE}:
            return "state_change"
        if existing.get("source_authority", "medium") == new.get("source_authority", "medium"):
            return "contradicts"
        return "uncertain"

    @staticmethod
    def _value(claim: dict[str, Any]) -> Any:
        return claim.get("value")

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
        qualifiers = claim.get("qualifiers") or {}
        if isinstance(qualifiers, str):
            try:
                qualifiers = json.loads(qualifiers)
            except json.JSONDecodeError:
                qualifiers = {}
        return bool(qualifiers.get("state_change") or qualifiers.get("current") or qualifiers.get("change"))
