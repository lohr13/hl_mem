"""Pure measurement basis and object identity helpers for temporal admission."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

_GENERIC_SUBJECTS = frozenset(
    {"user", "personuser", "用户", "assistant", "personassistant", "助手", "default", "unknown", "未知"}
)
_HOLDING_ENTITY = re.compile(r"持有[\d,]+(?:股|份)([\u4e00-\u9fff]{2,12}(?:ETF)?)", re.IGNORECASE)
_NAMED_PRICE_ENTITY = re.compile(
    r"(?:若|对|挂)([\u4e00-\u9fff]{2,12}(?:ETF)?)(?:的)?" r"(?:股价|价格|现价|收盘价|成本价|目标价)",
    re.IGNORECASE,
)
_TICKER_ENTITY = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,8})(?![A-Za-z0-9])")
_SECURITY_CODE_ENTITY = re.compile(r"(?:ETF|基金|代码|证券代码)[:：]?\s*(\d{6})(?!\d)", re.IGNORECASE)
_ENTITY_STOPWORDS = frozenset({"ETF", "IP", "MA", "CNY", "USD"})


def _measurement_basis(value: Any) -> tuple[str, str]:
    """Distinguish a price level from an amount accumulated over a period/event."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    accumulated = bool(
        re.search(r"\b(?:earned|earning|income|revenue|paid|spent|total|salary)\b|收入|赚了|共花|总计", text)
    )
    if accumulated and re.search(r"\b(?:annual|yearly)\b|年收入|全年", text):
        return "annual_total", ":".join(re.findall(r"\b(?:19|20)\d{2}\b", text))
    if accumulated and re.search(r"\bmonthly\b|月收入|本月收入", text):
        return "monthly_total", ""
    if accumulated:
        return "event_total", ""
    if re.search(r"\beach\b|\bper\s+(?:item|unit)\b|每件|每个", text):
        return "unit_price", ""
    return "price", ""


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


def _price_target_relation(existing: dict[str, Any], new: dict[str, Any]) -> Literal["same", "different", "missing"]:
    """Compare only persisted typed targets; one-sided projection is never a wildcard."""

    old_target = str(existing.get("canonical_target_entity_id") or "").strip()
    new_target = str(new.get("canonical_target_entity_id") or "").strip()
    if not old_target or not new_target:
        return "missing"
    return "same" if old_target == new_target else "different"


def _explicit_price_subjects(value: Any) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    found = {match.group(1) for pattern in (_HOLDING_ENTITY, _NAMED_PRICE_ENTITY) for match in pattern.finditer(text)}
    found.update(match.group(1) for match in _TICKER_ENTITY.finditer(text) if match.group(1) not in _ENTITY_STOPWORDS)
    found.update(match.group(1) for match in _SECURITY_CODE_ENTITY.finditer(text))
    return frozenset(key for item in found if (key := _normalize_entity_key(item)))


def _normalize_entity_key(value: Any) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())
    return normalized.removesuffix("etf")
