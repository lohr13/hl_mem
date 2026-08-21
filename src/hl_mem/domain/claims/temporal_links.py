"""Conservative deterministic links for explicitly observed temporal updates."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

TemporalLinkOutcome = Literal[
    "entails",
    "state_change",
    "distinct_series",
    "snapshot_advance",
    "uncertain",
    "not_applicable",
]
SnapshotOrder = Literal["newer", "older"]

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
_SERIES_PRICE_AXES = frozenset(
    {"spot", "close", "low", "high", "open", "average", "target", "cost_estimate", "generic_price"}
)
_SNAPSHOT_PRICE_AXES = frozenset({"spot", "close", "low", "high", "open", "average", "generic_price"})
_GENERIC_SUBJECTS = frozenset({"user", "用户", "assistant", "助手", "default", "unknown", "未知"})
_FULL_CHINESE_DATE = re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日(?!\d)")
_FULL_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_CONTEXTUAL_CHINESE_DATE = re.compile(r"(?<![\d年])(\d{1,2})月(\d{1,2})日(?!\d)")
_HOLDING_ENTITY = re.compile(r"持有[\d,]+(?:股|份)([\u4e00-\u9fff]{2,12}(?:ETF)?)", re.IGNORECASE)
_NAMED_PRICE_ENTITY = re.compile(
    r"(?:若|对|挂)([\u4e00-\u9fff]{2,12}(?:ETF)?)(?:的)?"
    r"(?:股价|价格|现价|收盘价|成本价|目标价)",
    re.IGNORECASE,
)
_TICKER_ENTITY = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,8})(?![A-Za-z0-9])")
_SECURITY_CODE_ENTITY = re.compile(r"(?:ETF|基金|代码|证券代码)[:：]?\s*(\d{6})(?!\d)", re.IGNORECASE)
_ENTITY_STOPWORDS = frozenset({"ETF", "IP", "MA", "CNY", "USD"})


@dataclass(frozen=True)
class TemporalLinkDecision:
    """One deterministic decision; ``not_applicable`` preserves legacy behavior."""

    outcome: TemporalLinkOutcome
    rule_id: str | None
    rationale: str
    snapshot_order: SnapshotOrder | None = None


def evaluate_temporal_link(existing: dict[str, Any], new: dict[str, Any]) -> TemporalLinkDecision:
    """Classify proven availability, explicit replacements, and ordered price snapshots."""

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
    if old_axis is None or new_axis is None:
        return TemporalLinkDecision("not_applicable", None, "no_proven_temporal_axis")

    if _REPLACEMENT_MARKER.search(str(new.get("value") or "")):
        return _evaluate_explicit_price_replacement(existing, new, old_axis, new_axis, old_text, new_text)

    series_decision = _evaluate_price_series(existing, new, old_axis, new_axis, old_text, new_text)
    if series_decision is not None:
        return series_decision
    return _evaluate_explicit_price_replacement(existing, new, old_axis, new_axis, old_text, new_text)


def _evaluate_explicit_price_replacement(
    existing: dict[str, Any],
    new: dict[str, Any],
    old_axis: str,
    new_axis: str,
    old_text: str,
    new_text: str,
) -> TemporalLinkDecision:
    if old_axis != new_axis:
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


def _evaluate_price_series(
    existing: dict[str, Any],
    new: dict[str, Any],
    old_axis: str,
    new_axis: str,
    old_text: str,
    new_text: str,
) -> TemporalLinkDecision | None:
    if old_axis not in _SERIES_PRICE_AXES or new_axis not in _SERIES_PRICE_AXES:
        return None
    if old_axis != new_axis:
        return TemporalLinkDecision(
            "distinct_series",
            f"{TEMPORAL_LINK_RULE_VERSION}:series-coordinate",
            f"price_measure_differs:{old_axis}:{new_axis}",
        )

    subject_relation = _price_subject_relation(existing, new)
    if subject_relation == "different":
        return TemporalLinkDecision(
            "distinct_series",
            f"{TEMPORAL_LINK_RULE_VERSION}:series-coordinate",
            "price_subject_differs",
        )
    if subject_relation == "missing":
        return TemporalLinkDecision(
            "uncertain",
            f"{TEMPORAL_LINK_RULE_VERSION}:snapshot-coordinate",
            "price_subject_missing",
        )
    if old_axis not in _SNAPSHOT_PRICE_AXES:
        return None

    rule_id = f"{TEMPORAL_LINK_RULE_VERSION}:snapshot-coordinate"
    if not _authority_sufficient(existing, new):
        return TemporalLinkDecision("uncertain", rule_id, "price_authority_downgrade")
    if not _compatible_currency_and_unit(old_text, new_text):
        return TemporalLinkDecision("uncertain", rule_id, "price_currency_or_unit_changed")
    old_coordinate = parse_snapshot_coordinate(existing.get("value"), existing.get("valid_from"))
    new_coordinate = parse_snapshot_coordinate(new.get("value"), new.get("valid_from"))
    if old_coordinate is None or new_coordinate is None:
        return TemporalLinkDecision("uncertain", rule_id, "snapshot_coordinate_missing")
    if new_coordinate == old_coordinate:
        return TemporalLinkDecision("uncertain", rule_id, "snapshot_coordinate_equal")
    if new_coordinate > old_coordinate:
        if not _strictly_newer(existing, new):
            return TemporalLinkDecision("uncertain", rule_id, "snapshot_valid_time_order_conflict")
        return TemporalLinkDecision(
            "snapshot_advance",
            rule_id,
            "snapshot_coordinate_advanced",
            snapshot_order="newer",
        )
    if not _strictly_newer(new, existing):
        return TemporalLinkDecision("uncertain", rule_id, "snapshot_valid_time_order_conflict")
    return TemporalLinkDecision(
        "snapshot_advance",
        rule_id,
        "snapshot_coordinate_precedes_existing",
        snapshot_order="older",
    )


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


def parse_snapshot_coordinate(value: Any, valid_from: Any) -> datetime | None:
    """Return one conservative explicit date, otherwise the bitemporal valid-time anchor."""

    fallback = _parse_time(valid_from)
    explicit = _explicit_snapshot_dates(str(value or ""), fallback.year if fallback is not None else None)
    if explicit is None:
        return None
    if explicit:
        return explicit[0] if len(set(explicit)) == 1 else None
    return fallback


def _explicit_snapshot_dates(value: str, contextual_year: int | None) -> tuple[datetime, ...] | None:
    normalized = unicodedata.normalize("NFKC", value)
    matches: list[tuple[int, int, int, tuple[int, int]]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in (_FULL_CHINESE_DATE, _FULL_ISO_DATE):
        for match in pattern.finditer(normalized):
            matches.append((int(match.group(1)), int(match.group(2)), int(match.group(3)), match.span()))
            occupied.append(match.span())
    for match in _CONTEXTUAL_CHINESE_DATE.finditer(normalized):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        if contextual_year is None:
            return None
        matches.append((contextual_year, int(match.group(1)), int(match.group(2)), match.span()))
    if not matches:
        return ()
    try:
        return tuple(datetime(year, month, day, tzinfo=timezone.utc) for year, month, day, _ in matches)
    except ValueError:
        return None


def _price_subject_relation(existing: dict[str, Any], new: dict[str, Any]) -> Literal["same", "different", "missing"]:
    old_entities = _explicit_price_subjects(existing.get("value"))
    new_entities = _explicit_price_subjects(new.get("value"))
    if old_entities and new_entities:
        return "same" if old_entities & new_entities else "different"
    subject = _normalize_entity_key(existing.get("subject_entity_id"))
    if not subject or subject in _GENERIC_SUBJECTS:
        return "missing"
    one_sided_entities = old_entities or new_entities
    return "missing" if one_sided_entities and subject not in one_sided_entities else "same"


def _explicit_price_subjects(value: Any) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    found = {match.group(1) for pattern in (_HOLDING_ENTITY, _NAMED_PRICE_ENTITY) for match in pattern.finditer(text)}
    found.update(match.group(1) for match in _TICKER_ENTITY.finditer(text) if match.group(1) not in _ENTITY_STOPWORDS)
    found.update(match.group(1) for match in _SECURITY_CODE_ENTITY.finditer(text))
    return frozenset(key for item in found if (key := _normalize_entity_key(item)))


def _normalize_entity_key(value: Any) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())
    return normalized.removesuffix("etf")


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
    if any(token in text for token in ("目标价", "targetprice")):
        return "target"
    if re.search(r"(?:ma\d+|\d+日均价|均价|移动平均|movingaverage|average)", text, re.IGNORECASE):
        return "average"
    if any(token in text for token in ("最低价", "最低", "低点", "lowprice")):
        return "low"
    if any(token in text for token in ("最高价", "最高", "高点", "highprice")):
        return "high"
    if any(token in text for token in ("开盘价", "开盘", "openprice")):
        return "open"
    if any(token in text for token in ("收盘价", "收盘", "昨收", "closeprice")):
        return "close"
    if any(token in text for token in ("现价", "当前价", "即时价", "市价", "股价", "spotprice")):
        return "spot"
    if any(token in text for token in ("旧估算", "重新估算", "费用约", "全量费用", "预计消耗", "成本估算", "成本价")):
        return "cost_estimate"
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
    if any(token in text for token in ("¥", "￥", "人民币", "cny")) or ("元" in text and "美元" not in text):
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
    if any(token in text for token in ("/股", "每股", "pershare")):
        found.add("per_share")
    if any(token in text for token in ("/份", "每份", "perunit")):
        found.add("per_unit")
    if any(token in text for token in ("/克", "每克", "pergram")):
        found.add("per_gram")
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
