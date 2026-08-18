"""Conservative deterministic links for explicitly observed temporal updates."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

TemporalLinkOutcome = Literal["entails", "state_change", "uncertain", "not_applicable"]

TEMPORAL_LINK_RULE_VERSION = "temporal-v1"
_DENIED_MULTI_VALUE_ATTRIBUTES = frozenset({"config.path", "config.network"})
_REPLACEMENT_MARKER = re.compile(
    r"(?:新价|旧价|旧成本|旧估算|作废|失效|无效|重新估算|不再|更正为|改为|调整为|更新为|"
    r"replace(?:s|d)?|no longer|obsolete|invalid)",
    re.IGNORECASE,
)
_CURRENCY_PREFIX_AMOUNT = re.compile(r"(?:¥|￥|\$|人民币|美元|cny|usd)(\d+(?:\.\d+)?)", re.IGNORECASE)
_CURRENCY_SUFFIX_AMOUNT = re.compile(r"(?<![a-z0-9])(\d+(?:\.\d+)?)(?:元|人民币|美元|cny|usd)", re.IGNORECASE)
_OLD_PRICE_CLAUSE = re.compile(
    r"(?:旧价|原价|旧成本(?:估算)?|旧估算|oldprice|previousprice)([^，。；;!！]{0,120})",
    re.IGNORECASE,
)
_AUTHORITY_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class TemporalLinkDecision:
    """One deterministic decision; ``not_applicable`` preserves legacy behavior."""

    outcome: TemporalLinkOutcome
    rule_id: str | None
    rationale: str


def evaluate_temporal_link(existing: dict[str, Any], new: dict[str, Any]) -> TemporalLinkDecision:
    """Classify only two proven segments: atomic availability and explicit price replacement."""

    guard = _guard(existing, new)
    if guard is not None:
        return guard

    old_text = _normalize_text(existing.get("value"))
    new_text = _normalize_text(new.get("value"))
    if old_text == new_text:
        return TemporalLinkDecision("entails", f"{TEMPORAL_LINK_RULE_VERSION}:exact-value", "same_value")

    old_availability = _atomic_availability(existing)
    new_availability = _atomic_availability(new)
    if old_availability is not None and new_availability is not None:
        rule_id = f"{TEMPORAL_LINK_RULE_VERSION}:atomic-availability"
        if old_availability == new_availability:
            return TemporalLinkDecision("entails", rule_id, "same_atomic_availability")
        if not _strictly_newer(existing, new):
            return TemporalLinkDecision("uncertain", rule_id, "availability_time_not_strictly_newer")
        if not _authority_sufficient(existing, new):
            return TemporalLinkDecision("uncertain", rule_id, "availability_authority_downgrade")
        return TemporalLinkDecision("state_change", rule_id, "newer_opposite_atomic_availability")

    old_axis = _price_axis(old_text)
    new_axis = _price_axis(new_text)
    if old_axis is None or new_axis is None or old_axis != new_axis:
        return TemporalLinkDecision("not_applicable", None, "no_proven_temporal_axis")

    rule_id = f"{TEMPORAL_LINK_RULE_VERSION}:explicit-price-replacement"
    if not _strictly_newer(existing, new):
        return TemporalLinkDecision("uncertain", rule_id, "price_time_not_strictly_newer")
    if not _authority_sufficient(existing, new):
        return TemporalLinkDecision("uncertain", rule_id, "price_authority_downgrade")
    if not _REPLACEMENT_MARKER.search(str(new.get("value") or "")):
        return TemporalLinkDecision("uncertain", rule_id, "price_replacement_not_explicit")
    if not _compatible_currency_and_unit(old_text, new_text):
        return TemporalLinkDecision("uncertain", rule_id, "price_currency_or_unit_changed")
    if _price_replacement_is_anchored(old_axis, old_text, new_text):
        return TemporalLinkDecision("state_change", rule_id, "explicit_old_price_anchor")
    return TemporalLinkDecision("uncertain", rule_id, "old_price_not_anchored")


def _guard(existing: dict[str, Any], new: dict[str, Any]) -> TemporalLinkDecision | None:
    if new.get("assertion_kind") != "observation":
        return TemporalLinkDecision("not_applicable", None, "new_assertion_kind_not_observation")
    identity_fields = ("namespace_key", "subject_entity_id", "predicate", "canonical_attribute")
    if any(existing.get(field) != new.get(field) for field in identity_fields):
        return TemporalLinkDecision("not_applicable", None, "series_identity_mismatch")
    attribute = str(new.get("canonical_attribute") or "")
    if attribute in _DENIED_MULTI_VALUE_ATTRIBUTES:
        return TemporalLinkDecision("not_applicable", None, "multi_value_attribute_denied")
    if existing.get("canonical_slot") is not None or new.get("canonical_slot") is not None:
        return TemporalLinkDecision("not_applicable", None, "operational_slot_uses_existing_resolver")
    if _canonical_json(existing.get("qualifiers") or {}) != _canonical_json(new.get("qualifiers") or {}):
        return TemporalLinkDecision("not_applicable", None, "qualifier_boundary_mismatch")
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _atomic_availability(claim: dict[str, Any]) -> Literal["online", "offline"] | None:
    value = _normalize_text(claim.get("value"))
    subject = _normalize_text(claim.get("subject_entity_id"))
    if subject and value.startswith(subject):
        value = value[len(subject) :]
    value = value.strip("。.!！,，:：;；()（）[]【】")
    prefix = r"(?:(?:当前|目前|现在|已经|已|整体|状态|当前状态|目前状态|处于|currently|overall|is))*"
    suffix = r"(?:(?:状态|state))?(?:\d+(?:天|days?))?"
    if re.fullmatch(prefix + r"(?:离线|不在线|offline|inactive)" + suffix, value, re.IGNORECASE):
        return "offline"
    if re.fullmatch(prefix + r"(?:在线|online|active|live)" + suffix, value, re.IGNORECASE):
        return "online"
    return None


def _price_axis(text: str) -> str | None:
    if not any(token in text for token in ("价", "费用", "成本", "¥", "￥", "cny", "usd", "$", "price", "cost")):
        return None
    if any(token in text for token in ("价格见底", "见底", "底价", "cheapest")):
        return "price_floor"
    if any(token in text for token in ("旧估算", "重新估算", "费用约", "全量费用", "预计消耗", "成本估算")):
        return "cost_estimate"
    has_input = any(token in text for token in ("输入", "input"))
    has_output = any(token in text for token in ("输出", "output"))
    if has_input and has_output:
        return "input_output_price"
    if has_input:
        return "input_price"
    if has_output:
        return "output_price"
    if any(token in text for token in ("峰谷", "忙时", "闲时", "白天晚上", "peak", "off-peak")):
        return "pricing_regime"
    if any(token in text for token in ("估算", "费用", "成本", "estimate")):
        return "cost_estimate"
    return "generic_price"


def _strictly_newer(existing: dict[str, Any], new: dict[str, Any]) -> bool:
    old_time = _parse_time(existing.get("valid_from"))
    new_time = _parse_time(new.get("valid_from"))
    return old_time is not None and new_time is not None and new_time > old_time


def _authority_sufficient(existing: dict[str, Any], new: dict[str, Any]) -> bool:
    old_rank = _AUTHORITY_RANK.get(str(existing.get("source_authority") or "medium"), 1)
    new_rank = _AUTHORITY_RANK.get(str(new.get("source_authority") or "medium"), 1)
    return new_rank >= old_rank


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _compatible_currency_and_unit(old_text: str, new_text: str) -> bool:
    old_currencies = _currencies(old_text)
    new_currencies = _currencies(new_text)
    if old_currencies and new_currencies != old_currencies:
        return False
    old_units = _billing_units(old_text)
    new_units = _billing_units(new_text)
    return not (old_units and new_units != old_units)


def _currencies(text: str) -> frozenset[str]:
    found: set[str] = set()
    if any(token in text for token in ("¥", "￥", "人民币", "cny")):
        found.add("cny")
    if any(token in text for token in ("$", "美元", "usd")):
        found.add("usd")
    return frozenset(found)


def _billing_units(text: str) -> frozenset[str]:
    found: set[str] = set()
    if any(token in text for token in ("每百万token", "/百万token", "permilliontoken")):
        found.add("per_million_token")
    if any(token in text for token in ("/月", "每月", "permonth")):
        found.add("per_month")
    if any(token in text for token in ("/年", "每年", "peryear")):
        found.add("per_year")
    return frozenset(found)


def _price_replacement_is_anchored(axis: str, old_text: str, new_text: str) -> bool:
    if axis == "price_floor":
        return (
            "见底" in old_text
            and "见底" in new_text
            and any(marker in new_text for marker in ("失效", "无效", "作废", "不再"))
        )
    if axis == "pricing_regime":
        old_negative = "没有峰谷" in old_text or "一样" in old_text
        new_opposite = "实行峰谷" in new_text or "不再一样" in new_text
        return old_negative and new_opposite
    old_price_amounts = _price_amount_anchors(old_text)
    explicit_old_amounts = _explicit_old_price_anchors(new_text)
    return bool(old_price_amounts) and old_price_amounts <= explicit_old_amounts


def _price_amount_anchors(text: str) -> frozenset[str]:
    """Return only currency-qualified amounts, never unrelated counts or dates."""

    return frozenset(
        match.group(1)
        for pattern in (_CURRENCY_PREFIX_AMOUNT, _CURRENCY_SUFFIX_AMOUNT)
        for match in pattern.finditer(text)
    )


def _explicit_old_price_anchors(text: str) -> frozenset[str]:
    """Return amounts inside a clause explicitly identifying the replaced old price."""

    return frozenset(
        anchor for clause in _OLD_PRICE_CLAUSE.finditer(text) for anchor in _price_amount_anchors(clause.group(0))
    )
