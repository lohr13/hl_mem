"""Pure recognition of existing typed financial-instrument coordinates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Sequence

InstrumentResolutionOutcome = Literal["resolved", "unresolved", "ambiguous"]

_CN_SUFFIX = re.compile(r"(?<!\d)(\d{6})\.(SH|SZ|BJ)(?![A-Za-z0-9])", re.IGNORECASE)
_CN_PREFIX = re.compile(r"(?<![A-Za-z0-9])(SH|SZ|BJ)[:.]?(\d{6})(?!\d)", re.IGNORECASE)
_CN_BARE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_QUALIFIED_TICKER = re.compile(r"(?<![A-Za-z0-9])(NASDAQ|NYSE|AMEX|HKEX)[:.]([A-Z][A-Z0-9.-]{1,9})", re.IGNORECASE)
_BARE_TICKER = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,8})(?![A-Za-z0-9])")
_TICKER_STOPWORDS = frozenset({"ETF", "CNY", "USD", "RMB", "PRICE", "CLOSE", "OPEN"})


@dataclass(frozen=True, slots=True)
class InstrumentReference:
    canonical_entity_id: str
    canonical_key: str
    aliases: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class InstrumentTarget:
    outcome: InstrumentResolutionOutcome
    mention: str | None = None
    canonical_entity_id: str | None = None
    alias_version: int | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class _Match:
    reference: InstrumentReference
    mention: str
    alias_version: int | None
    source: str


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _cn_market(code: str) -> str | None:
    if code[0] in {"5", "6", "9"}:
        return "SH"
    if code[0] in {"0", "1", "2", "3"}:
        return "SZ"
    if code[0] in {"4", "8"}:
        return "BJ"
    return None


def _key_matches(text: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in _CN_SUFFIX.finditer(text):
        code, market = match.group(1), match.group(2).upper()
        matches.append((f"CN:{market}:{code}", _normalize(match.group(0))))
        occupied.append(match.span())
    for match in _CN_PREFIX.finditer(text):
        market, code = match.group(1).upper(), match.group(2)
        matches.append((f"CN:{market}:{code}", _normalize(match.group(0))))
        occupied.append(match.span())
    for match in _QUALIFIED_TICKER.finditer(text):
        exchange, ticker = match.group(1).upper(), match.group(2).upper()
        market = "HK" if exchange == "HKEX" else "US"
        matches.append((f"{market}:{exchange}:{ticker}", _normalize(match.group(0))))
        occupied.append(match.span())
    for match in _CN_BARE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        code = match.group(1)
        market = _cn_market(code)
        if market:
            matches.append((f"CN:{market}:{code}", code))
    return matches


def _alias_version(reference: InstrumentReference, mention: str) -> int | None:
    normalized = _normalize(mention)
    return next((version for alias, version in reference.aliases if _normalize(alias) == normalized), None)


def _matches(text: str, references: Sequence[InstrumentReference]) -> list[_Match]:
    found: list[_Match] = []
    for key, mention in _key_matches(text):
        for reference in references:
            if reference.canonical_key == key:
                found.append(_Match(reference, mention, _alias_version(reference, mention), "exact_code"))
    normalized_text = _normalize(text)
    for reference in references:
        for alias, version in reference.aliases:
            normalized_alias = _normalize(alias)
            if len(normalized_alias) < 2 or normalized_alias in {item.casefold() for item in _TICKER_STOPWORDS}:
                continue
            if normalized_alias in normalized_text:
                found.append(_Match(reference, normalized_alias, version, "typed_alias"))
    return found


def resolve_instrument_target(text: str, references: Sequence[InstrumentReference]) -> InstrumentTarget:
    """Resolve only one existing instrument; bare short tickers never select a market."""

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    matches = _matches(normalized, references)
    target_ids = {match.reference.canonical_entity_id for match in matches}
    if len(target_ids) > 1:
        return InstrumentTarget("ambiguous")
    if not target_ids:
        bare = {match.group(1) for match in _BARE_TICKER.finditer(normalized)} - _TICKER_STOPWORDS
        return InstrumentTarget("unresolved", mention=next(iter(sorted(bare)), None))
    selected = min(
        (match for match in matches if match.reference.canonical_entity_id in target_ids),
        key=lambda item: (0 if item.source == "exact_code" else 1, -len(item.mention), item.mention),
    )
    return InstrumentTarget(
        "resolved",
        selected.mention,
        selected.reference.canonical_entity_id,
        selected.alias_version,
        selected.source,
    )
