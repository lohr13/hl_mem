"""Pure, conservative action coordinates for plans and observed outcomes."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping, cast

ActionFamily = Literal["open", "increase", "reduce", "close", "deliver", "deploy", "publish", "fix", "other_controlled"]
Direction = Literal["long", "short", "in", "out", "neutral"]
QuantityMode = Literal["exact", "all", "unknown"]
AssertionPhase = Literal["plan", "execution", "cancellation", "replacement"]

_ACTION_FAMILIES: tuple[tuple[ActionFamily, re.Pattern[str]], ...] = (
    ("close", re.compile(r"(?:清仓|全部卖出|卖光|close\s+(?:the\s+)?position)", re.IGNORECASE)),
    ("reduce", re.compile(r"(?:减持|部分卖出|卖出|reduce|trim)", re.IGNORECASE)),
    ("increase", re.compile(r"(?:增持|加仓|increase|add\s+to)", re.IGNORECASE)),
    ("open", re.compile(r"(?:买入|建仓|开仓|open\s+(?:a\s+)?position|\bbuy\b)", re.IGNORECASE)),
    ("deliver", re.compile(r"(?:交付|deliver)", re.IGNORECASE)),
    ("deploy", re.compile(r"(?:部署|deploy)", re.IGNORECASE)),
    ("publish", re.compile(r"(?:发布|publish|release)", re.IGNORECASE)),
    ("fix", re.compile(r"(?:修复|fix|repair)", re.IGNORECASE)),
)
_CANCELLATION = re.compile(r"(?:取消|撤销|不再执行|cancel|abort)", re.IGNORECASE)
_REPLACEMENT = re.compile(r"(?:改为|替换为|取代|replace(?:d)?\s+by|instead)", re.IGNORECASE)
_EXECUTION = re.compile(r"(?:已|已经|完成|执行|成交|filled|completed|executed)", re.IGNORECASE)
_ALL_QUANTITY = re.compile(r"(?:全部|全仓|清仓|卖光|all)", re.IGNORECASE)
_QUANTITY = re.compile(
    r"(?<![\d.])(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(股|份|克|千克|公斤|kg|g|shares?|units?)(?![A-Za-z])",
    re.IGNORECASE,
)
_UNIT_MAP = {
    "股": "share",
    "份": "share",
    "share": "share",
    "shares": "share",
    "unit": "unit",
    "units": "unit",
    "克": "gram",
    "g": "gram",
    "千克": "kilogram",
    "公斤": "kilogram",
    "kg": "kilogram",
}


@dataclass(frozen=True, slots=True)
class QuantityCoordinate:
    mode: QuantityMode
    amount: Decimal | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class PlanCoordinate:
    namespace: str
    canonical_target_entity_id: str
    action_family: ActionFamily
    direction: Direction
    quantity: QuantityCoordinate
    account: str | None
    window_start: str
    window_end: str | None
    assertion_phase: AssertionPhase


def decimal_text(value: Decimal) -> str:
    """Return a non-exponent Decimal representation with insignificant zeros removed."""

    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", ""} else text


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _action_family(text: str) -> ActionFamily | None:
    return next((family for family, pattern in _ACTION_FAMILIES if pattern.search(text)), None)


def _direction(text: str, family: ActionFamily) -> Direction:
    if re.search(r"(?:做空|卖空|short)", text, re.IGNORECASE):
        return "short"
    if family in {"open", "increase"}:
        return "long"
    if family in {"reduce", "close"}:
        return "out"
    if family in {"deliver", "deploy", "publish", "fix"}:
        return "in"
    return "neutral"


def _phase(text: str, is_plan: bool) -> AssertionPhase:
    if _CANCELLATION.search(text):
        return "cancellation"
    if _REPLACEMENT.search(text):
        return "replacement"
    if is_plan:
        return "plan"
    return "execution" if _EXECUTION.search(text) else "execution"


def _quantity_fields(text: str) -> dict[str, str]:
    if _ALL_QUANTITY.search(text):
        return {"quantity_mode": "all"}
    match = _QUANTITY.search(text)
    if match is None:
        return {"quantity_mode": "unknown"}
    amount = _decimal(match.group(1))
    unit = _UNIT_MAP.get(match.group(2).casefold())
    if amount is None or unit is None:
        return {"quantity_mode": "unknown"}
    return {"quantity_mode": "exact", "quantity": decimal_text(amount), "quantity_unit": unit}


def project_action_qualifiers(
    value: str,
    qualifiers: Mapping[str, Any] | None,
    *,
    is_plan: bool,
) -> dict[str, Any]:
    """Project only source-visible controlled action fields; existing explicit fields win."""

    projected = dict(qualifiers or {})
    text = unicodedata.normalize("NFKC", str(value or ""))
    family = _action_family(text)
    if family is None:
        return projected
    projected.setdefault("action_family", family)
    projected.setdefault("direction", _direction(text, family))
    projected.setdefault("assertion_phase", _phase(text, is_plan))
    for key, item in _quantity_fields(text).items():
        projected.setdefault(key, item)
    return projected


def _quantity_from_qualifiers(qualifiers: Mapping[str, Any]) -> QuantityCoordinate | None:
    mode = str(qualifiers.get("quantity_mode") or "unknown")
    if mode == "all":
        return QuantityCoordinate("all", None, None)
    if mode != "exact":
        return None
    amount = _decimal(qualifiers.get("quantity"))
    unit = str(qualifiers.get("quantity_unit") or "").strip().casefold()
    if amount is None or not unit:
        return None
    return QuantityCoordinate("exact", amount, unit)


def coordinate_from_claim(claim: Mapping[str, Any]) -> PlanCoordinate | None:
    """Build one strict coordinate or fail closed when a protected field is incomplete."""

    qualifiers = claim.get("qualifiers")
    if not isinstance(qualifiers, Mapping):
        return None
    namespace = str(claim.get("namespace_key") or "").strip()
    target = str(claim.get("canonical_target_entity_id") or "").strip()
    family = str(qualifiers.get("action_family") or "")
    direction = str(qualifiers.get("direction") or "")
    phase = str(qualifiers.get("assertion_phase") or "")
    window_start = str(claim.get("occurred_start") or claim.get("valid_from") or "").strip()
    quantity = _quantity_from_qualifiers(qualifiers)
    if (
        not namespace
        or not target
        or family not in {item for item, _ in _ACTION_FAMILIES}
        or direction not in {"long", "short", "in", "out", "neutral"}
        or phase not in {"plan", "execution", "cancellation", "replacement"}
        or not window_start
        or quantity is None
    ):
        return None
    account_value = qualifiers.get("account")
    account = None if account_value is None else str(account_value).strip()
    if account == "":
        return None
    return PlanCoordinate(
        namespace,
        target,
        family,
        cast(Direction, direction),
        quantity,
        account,
        window_start,
        str(claim.get("occurred_end") or "").strip() or None,
        cast(AssertionPhase, phase),
    )
