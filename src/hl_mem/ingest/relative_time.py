"""基于显式事件时间的中英文绝对/相对日期解析。"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

_ABSOLUTE_DATE_RE = re.compile(
    r"(?P<year>\d{4})(?:-|/|年)(?P<month>\d{1,2})(?:-|/|月)(?P<day>\d{1,2})日?"
    r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"(?P<timezone>Z|[+-]\d{2}:?\d{2})?)?(?![T\d:+-])"
)
_EN_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_EN_MONTH_DATE_RE = re.compile(
    rf"(?i)\b(?P<month>{'|'.join(_EN_MONTHS)})\.?\s+" r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(?P<year>\d{4}))?\b"
)
_US_NUMERIC_DATE_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})(?!\d)")
_FIXED_DAY_RE = re.compile(
    r"(?i)(?P<en>\b(?:the day before yesterday|day before yesterday|yesterday|today|tomorrow|"
    r"the day after tomorrow|day after tomorrow)\b)|(?P<zh>前天|昨天|今天|明天|后天)"
)
_EN_DIGIT_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_EN_NUMBER = rf"(?:{_EN_DIGIT_NUMBER}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
_EN_NUMBER += r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
_EN_NUMBER += r"sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?"
_EN_OFFSET_RE = re.compile(
    rf"(?ix)(?<![\w,])(?:(?P<ago_number>{_EN_NUMBER})\s+"
    rf"(?P<ago_unit>days?|weeks?|months?|years?)\s+ago|"
    rf"in\s+(?P<future_number>{_EN_NUMBER})\s+"
    rf"(?P<future_unit>days?|weeks?|months?|years?))(?![\w,])"
)
_ZH_OFFSET_RE = re.compile(
    r"(?P<number>\d+|[零〇一二两三四五六七八九十]+)个?(?P<unit>天|周|星期|个月|月|年)(?P<direction>前|后)"
)
_EN_WEEK_RE = re.compile(r"(?i)\b(?P<direction>last|this|next)\s+week\b")
_ZH_WEEK_RE = re.compile(r"(?P<direction>上|本|这|下)周(?![一二三四五六日天])")
_EN_WEEKDAY_RE = re.compile(
    r"(?i)\b(?P<direction>last|this|next)\s+" r"(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_ZH_WEEKDAY_RE = re.compile(r"(?P<direction>上|本|这|下)周(?P<weekday>[一二三四五六日天])")
_RANGE_LEAD_RE = re.compile(r"(?i)(?P<lead>\bfrom|\bbetween|从)\s*$")
_DIRECT_RANGE_SEPARATOR_RE = re.compile(r"(?i)^\s*(?:to|through|until|至|到|[-–—])\s*$")
_BETWEEN_SEPARATOR_RE = re.compile(r"(?i)^\s*(?:and|和|与|及|至|到)\s*$")

_EN_NUMBERS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_ZH_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_FIXED_DAY_OFFSETS = {
    "the day before yesterday": -2,
    "day before yesterday": -2,
    "yesterday": -1,
    "today": 0,
    "tomorrow": 1,
    "the day after tomorrow": 2,
    "day after tomorrow": 2,
    "前天": -2,
    "昨天": -1,
    "今天": 0,
    "明天": 1,
    "后天": 2,
}
_EN_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_ZH_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_MAX_CONVERSATION_RELATIVE_YEARS = 999


@dataclass(frozen=True)
class _TemporalMatch:
    """文本中的一个时间表达及其精度区间。"""

    start: int
    end: int
    occurred_start: datetime
    occurred_end: datetime | None


def relative_time_rules_fingerprint() -> dict[str, Any]:
    """返回影响时间后处理结果的稳定规则。"""
    return {
        "revision": 4,
        "patterns": {
            "absolute": _ABSOLUTE_DATE_RE.pattern,
            "english_month_date": _EN_MONTH_DATE_RE.pattern,
            "us_numeric_date": _US_NUMERIC_DATE_RE.pattern,
            "fixed_day": _FIXED_DAY_RE.pattern,
            "english_offset": _EN_OFFSET_RE.pattern,
            "chinese_offset": _ZH_OFFSET_RE.pattern,
            "english_week": _EN_WEEK_RE.pattern,
            "chinese_week": _ZH_WEEK_RE.pattern,
            "english_weekday": _EN_WEEKDAY_RE.pattern,
            "chinese_weekday": _ZH_WEEKDAY_RE.pattern,
            "range_lead": _RANGE_LEAD_RE.pattern,
            "direct_range_separator": _DIRECT_RANGE_SEPARATOR_RE.pattern,
            "between_separator": _BETWEEN_SEPARATOR_RE.pattern,
        },
        "english_months": _EN_MONTHS,
        "english_numbers": _EN_NUMBERS,
        "chinese_digits": _ZH_DIGITS,
        "fixed_day_offsets": _FIXED_DAY_OFFSETS,
        "english_weekdays": _EN_WEEKDAYS,
        "chinese_weekdays": _ZH_WEEKDAYS,
        "date_interval": "half_open_local_day",
        "week_start": "monday",
        "max_conversation_relative_years": _MAX_CONVERSATION_RELATIVE_YEARS,
    }


def _parse_base(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _start_of_day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _date_match(start: int, end: int, value: datetime) -> _TemporalMatch:
    occurred_start = _start_of_day(value)
    return _TemporalMatch(start, end, occurred_start, occurred_start + timedelta(days=1))


def _english_number(value: str) -> int | None:
    normalized = value.casefold().replace(",", "").replace("-", " ")
    if normalized.isdigit():
        return int(normalized)
    parts = normalized.split()
    if not parts or any(part not in _EN_NUMBERS for part in parts):
        return None
    return sum(_EN_NUMBERS[part] for part in parts)


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if "十" not in value:
        digits = [_ZH_DIGITS.get(character) for character in value]
        if any(digit is None for digit in digits):
            return None
        return int("".join(str(digit) for digit in digits))
    left, right = value.split("十", 1)
    tens = _ZH_DIGITS.get(left, 1) if left else 1
    ones = _ZH_DIGITS.get(right, 0) if right else 0
    return tens * 10 + ones


def _shift_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    if not datetime.min.year <= year <= datetime.max.year:
        raise OverflowError(f"shifted year {year} is outside datetime range")
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _apply_offset(value: datetime, amount: int, unit: str) -> datetime:
    normalized = unit.casefold()
    if normalized.startswith(("day", "天")):
        return value + timedelta(days=amount)
    if normalized.startswith(("week", "周", "星期")):
        return value + timedelta(weeks=amount)
    if normalized.startswith(("month", "月", "个月")):
        return _shift_months(value, amount)
    return _shift_months(value, amount * 12)


def _is_narrative_year_offset(amount: int, unit: str) -> bool:
    """避免把历史/叙事年龄强制解释为对话相对时间。"""
    normalized = unit.casefold()
    return normalized.startswith(("year", "年")) and abs(amount) > _MAX_CONVERSATION_RELATIVE_YEARS


def _qualified_weekday(value: datetime, target: int, direction: str) -> datetime:
    normalized = direction.casefold()
    if normalized in {"next", "下"}:
        days = (target - value.weekday()) % 7 or 7
        return value + timedelta(days=days)
    if normalized in {"last", "上"}:
        days = (value.weekday() - target) % 7 or 7
        return value - timedelta(days=days)
    return value + timedelta(days=target - value.weekday())


def _week_match(start: int, end: int, base: datetime, offset: int) -> _TemporalMatch:
    this_monday = _start_of_day(base) - timedelta(days=base.weekday())
    occurred_start = this_monday + timedelta(weeks=offset)
    return _TemporalMatch(start, end, occurred_start, occurred_start + timedelta(days=7))


def _relative_matches(text: str, base: datetime) -> list[_TemporalMatch]:
    matches: list[_TemporalMatch] = []
    for match in _FIXED_DAY_RE.finditer(text):
        phrase = (match.group("en") or match.group("zh")).casefold()
        try:
            matches.append(_date_match(match.start(), match.end(), base + timedelta(days=_FIXED_DAY_OFFSETS[phrase])))
        except (OverflowError, ValueError):
            continue
    for match in _EN_OFFSET_RE.finditer(text):
        number_text = match.group("ago_number") or match.group("future_number")
        unit = match.group("ago_unit") or match.group("future_unit")
        amount = _english_number(number_text)
        if amount is not None and not _is_narrative_year_offset(amount, unit):
            try:
                moment = _apply_offset(base, -amount if match.group("ago_number") else amount, unit)
                matches.append(_date_match(match.start(), match.end(), moment))
            except (OverflowError, ValueError):
                continue
    for match in _ZH_OFFSET_RE.finditer(text):
        amount = _chinese_number(match.group("number"))
        if amount is not None and not _is_narrative_year_offset(amount, match.group("unit")):
            direction = -1 if match.group("direction") == "前" else 1
            try:
                moment = _apply_offset(base, amount * direction, match.group("unit"))
                matches.append(_date_match(match.start(), match.end(), moment))
            except (OverflowError, ValueError):
                continue
    for match in _EN_WEEK_RE.finditer(text):
        offset = {"last": -1, "this": 0, "next": 1}[match.group("direction").casefold()]
        try:
            matches.append(_week_match(match.start(), match.end(), base, offset))
        except (OverflowError, ValueError):
            continue
    for match in _ZH_WEEK_RE.finditer(text):
        offset = {"上": -1, "本": 0, "这": 0, "下": 1}[match.group("direction")]
        try:
            matches.append(_week_match(match.start(), match.end(), base, offset))
        except (OverflowError, ValueError):
            continue
    for match in _EN_WEEKDAY_RE.finditer(text):
        target = _EN_WEEKDAYS[match.group("weekday").casefold()]
        try:
            moment = _qualified_weekday(base, target, match.group("direction"))
            matches.append(_date_match(match.start(), match.end(), moment))
        except (OverflowError, ValueError):
            continue
    for match in _ZH_WEEKDAY_RE.finditer(text):
        target = _ZH_WEEKDAYS[match.group("weekday")]
        try:
            moment = _qualified_weekday(base, target, match.group("direction"))
            matches.append(_date_match(match.start(), match.end(), moment))
        except (OverflowError, ValueError):
            continue
    return matches


def _absolute_matches(text: str, base: datetime | None) -> list[_TemporalMatch]:
    timezone_info = base.tzinfo if base is not None else timezone.utc
    matches: list[_TemporalMatch] = []
    for match in _ABSOLUTE_DATE_RE.finditer(text):
        has_time = match.group("hour") is not None
        try:
            matched_timezone = match.group("timezone")
            match_timezone: tzinfo | None
            if matched_timezone == "Z":
                match_timezone = timezone.utc
            elif matched_timezone:
                compact_offset = matched_timezone.replace(":", "")
                direction = 1 if compact_offset[0] == "+" else -1
                offset_hours = int(compact_offset[1:3])
                offset_minutes = int(compact_offset[3:5])
                if offset_hours > 23 or offset_minutes > 59:
                    raise ValueError("invalid timezone offset")
                offset = timedelta(
                    hours=offset_hours,
                    minutes=offset_minutes,
                )
                match_timezone = timezone(direction * offset)
            else:
                match_timezone = timezone_info
            moment = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour") or 0),
                int(match.group("minute") or 0),
                int(match.group("second") or 0),
                tzinfo=match_timezone,
            )
        except ValueError:
            continue
        if has_time:
            matches.append(_TemporalMatch(match.start(), match.end(), moment, None))
        else:
            matches.append(_date_match(match.start(), match.end(), moment))
    for match in _EN_MONTH_DATE_RE.finditer(text):
        raw_year = match.group("year")
        if raw_year is None:
            if base is None:
                continue
            year = base.year
        else:
            year = int(raw_year)
        try:
            moment = datetime(
                year,
                _EN_MONTHS[match.group("month").casefold()],
                int(match.group("day")),
                tzinfo=timezone_info,
            )
        except ValueError:
            continue
        matches.append(_date_match(match.start(), match.end(), moment))
    for match in _US_NUMERIC_DATE_RE.finditer(text):
        try:
            moment = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                tzinfo=timezone_info,
            )
        except ValueError:
            continue
        matches.append(_date_match(match.start(), match.end(), moment))
    return matches


def _non_overlapping(matches: list[_TemporalMatch]) -> list[_TemporalMatch]:
    ordered = sorted(matches, key=lambda item: (item.start, -(item.end - item.start)))
    selected: list[_TemporalMatch] = []
    for match in ordered:
        if selected and match.start < selected[-1].end:
            continue
        selected.append(match)
    return selected


def _range_pair(text: str, matches: list[_TemporalMatch]) -> tuple[_TemporalMatch, _TemporalMatch] | None:
    for left, right in zip(matches, matches[1:]):
        separator = text[left.end : right.start]
        lead_match = _RANGE_LEAD_RE.search(text[: left.start])
        if lead_match is None:
            is_range = _DIRECT_RANGE_SEPARATOR_RE.fullmatch(separator) is not None
        elif lead_match.group("lead").casefold() == "between":
            is_range = _BETWEEN_SEPARATOR_RE.fullmatch(separator) is not None
        else:
            is_range = _DIRECT_RANGE_SEPARATOR_RE.fullmatch(separator) is not None
        if not is_range:
            continue
        right_boundary = right.occurred_end or right.occurred_start
        if right_boundary > left.occurred_start:
            return left, right
    return None


def _all_matches(text: str, base: datetime | None) -> list[_TemporalMatch]:
    matches = _absolute_matches(text, base)
    if base is not None:
        matches.extend(_relative_matches(text, base))
    return _non_overlapping(matches)


def _match_interval(match: _TemporalMatch) -> tuple[str, str | None]:
    return (
        match.occurred_start.isoformat(),
        match.occurred_end.isoformat() if match.occurred_end is not None else None,
    )


def infer_occurrence(
    text: str,
    occurred_at: str | None,
    *,
    claim_value: str | None = None,
) -> tuple[str | None, str | None]:
    """从 evidence 推断时间区间；多日期必须为范围或由 claim value 唯一定位。"""
    base = _parse_base(occurred_at)
    ordered = _all_matches(text, base)
    if not ordered:
        return None, None
    explicit_range = _range_pair(text, ordered)
    if explicit_range is not None:
        left, right = explicit_range
        right_boundary = right.occurred_end or right.occurred_start
        return left.occurred_start.isoformat(), right_boundary.isoformat()
    if len(ordered) == 1:
        return _match_interval(ordered[0])
    if claim_value:
        claim_matches = _all_matches(claim_value, base)
        if len(claim_matches) == 1:
            target = _match_interval(claim_matches[0])
            if target in {_match_interval(match) for match in ordered}:
                return target
    return None, None
