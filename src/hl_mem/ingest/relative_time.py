"""基于显式事件时间的中英文绝对/相对日期解析。"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

_ABSOLUTE_DATE_RE = re.compile(
    r"(?P<year>\d{4})(?:-|/|年)(?P<month>\d{1,2})(?:-|/|月)(?P<day>\d{1,2})日?"
    r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
_FIXED_DAY_RE = re.compile(
    r"(?i)(?P<en>\b(?:the day before yesterday|day before yesterday|yesterday|today|tomorrow|"
    r"the day after tomorrow|day after tomorrow)\b)|(?P<zh>前天|昨天|今天|明天|后天)"
)
_EN_NUMBER = r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
_EN_NUMBER += r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
_EN_NUMBER += r"sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?"
_EN_OFFSET_RE = re.compile(
    rf"(?ix)\b(?:(?P<ago_number>{_EN_NUMBER})\s+(?P<ago_unit>days?|weeks?|months?|years?)\s+ago|"
    rf"in\s+(?P<future_number>{_EN_NUMBER})\s+(?P<future_unit>days?|weeks?|months?|years?))\b"
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


def relative_time_rules_fingerprint() -> dict[str, Any]:
    """返回影响相对时间后处理结果的稳定规则。"""
    return {
        "patterns": {
            "absolute": _ABSOLUTE_DATE_RE.pattern,
            "fixed_day": _FIXED_DAY_RE.pattern,
            "english_offset": _EN_OFFSET_RE.pattern,
            "chinese_offset": _ZH_OFFSET_RE.pattern,
            "english_week": _EN_WEEK_RE.pattern,
            "chinese_week": _ZH_WEEK_RE.pattern,
            "english_weekday": _EN_WEEKDAY_RE.pattern,
            "chinese_weekday": _ZH_WEEKDAY_RE.pattern,
        },
        "english_numbers": _EN_NUMBERS,
        "chinese_digits": _ZH_DIGITS,
        "fixed_day_offsets": _FIXED_DAY_OFFSETS,
        "english_weekdays": _EN_WEEKDAYS,
        "chinese_weekdays": _ZH_WEEKDAYS,
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


def _english_number(value: str) -> int | None:
    normalized = value.casefold().replace("-", " ")
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


def _qualified_weekday(value: datetime, target: int, direction: str) -> datetime:
    normalized = direction.casefold()
    if normalized in {"next", "下"}:
        days = (target - value.weekday()) % 7 or 7
        return value + timedelta(days=days)
    if normalized in {"last", "上"}:
        days = (value.weekday() - target) % 7 or 7
        return value - timedelta(days=days)
    return value + timedelta(days=target - value.weekday())


def _relative_candidates(text: str, base: datetime) -> list[tuple[int, datetime]]:
    candidates: list[tuple[int, datetime]] = []
    for match in _FIXED_DAY_RE.finditer(text):
        phrase = (match.group("en") or match.group("zh")).casefold()
        candidates.append((match.start(), base + timedelta(days=_FIXED_DAY_OFFSETS[phrase])))
    for match in _EN_OFFSET_RE.finditer(text):
        number_text = match.group("ago_number") or match.group("future_number")
        unit = match.group("ago_unit") or match.group("future_unit")
        amount = _english_number(number_text)
        if amount is not None:
            candidates.append(
                (match.start(), _apply_offset(base, -amount if match.group("ago_number") else amount, unit))
            )
    for match in _ZH_OFFSET_RE.finditer(text):
        amount = _chinese_number(match.group("number"))
        if amount is not None:
            direction = -1 if match.group("direction") == "前" else 1
            candidates.append((match.start(), _apply_offset(base, amount * direction, match.group("unit"))))
    for match in _EN_WEEK_RE.finditer(text):
        days = {"last": -7, "this": 0, "next": 7}[match.group("direction").casefold()]
        candidates.append((match.start(), base + timedelta(days=days)))
    for match in _ZH_WEEK_RE.finditer(text):
        days = {"上": -7, "本": 0, "这": 0, "下": 7}[match.group("direction")]
        candidates.append((match.start(), base + timedelta(days=days)))
    for match in _EN_WEEKDAY_RE.finditer(text):
        target = _EN_WEEKDAYS[match.group("weekday").casefold()]
        candidates.append((match.start(), _qualified_weekday(base, target, match.group("direction"))))
    for match in _ZH_WEEKDAY_RE.finditer(text):
        target = _ZH_WEEKDAYS[match.group("weekday")]
        candidates.append((match.start(), _qualified_weekday(base, target, match.group("direction"))))
    return candidates


def _absolute_moments(text: str, timezone_factory: Callable[..., datetime]) -> list[datetime]:
    moments: list[datetime] = []
    for match in _ABSOLUTE_DATE_RE.finditer(text):
        try:
            moment = timezone_factory(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour") or 0),
                int(match.group("minute") or 0),
                int(match.group("second") or 0),
            )
        except ValueError:
            continue
        if moment not in moments:
            moments.append(moment)
    return moments


def infer_occurrence(text: str, occurred_at: str | None) -> tuple[str | None, str | None]:
    """从 evidence 文本推断日期；相对表达只使用显式事件时间。"""
    base = _parse_base(occurred_at)
    tz = base.tzinfo if base is not None else timezone.utc

    def make_datetime(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime:
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)

    moments = _absolute_moments(text, make_datetime)
    if moments:
        return moments[0].isoformat(), moments[1].isoformat() if len(moments) > 1 else None
    if base is None:
        return None, None
    candidates = _relative_candidates(text, base)
    if not candidates:
        return None, None
    _position, moment = min(candidates, key=lambda item: item[0])
    return _start_of_day(moment).isoformat(), None
