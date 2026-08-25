"""Grounded, controlled lesson signals for notability and retention policy."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from hl_mem.observability.audit import current_audit

LessonSignal = Literal[
    "explicit_correction",
    "reusable_guardrail",
    "high_cost_failure",
    "persistent_must",
    "persistent_must_not",
    "none",
]
LessonSignalMode = Literal["off", "observe", "enforce"]

_GUIDANCE = {
    "en": """Additional notability policy:
- grounded user corrections, reusable guardrails, costly failures, and persistent must/must-not instructions are high;
- lesson, pitfall, or caution alone are not high; evidence_quote must locate the concrete signal;
- temporary, one-off, or deadline-bound claims remain temporal even when high.""",
    "zh": """附加 notability 规则：
- 有原文证据的用户纠正、可复用防错约束、高成本失败教训和持久必须/禁止指令为 high；
- 仅出现“教训”“踩坑”“注意”不是 high，evidence_quote 必须定位具体信号；
- 明确临时、一次性或有截止日期的内容即使 high 仍保持 temporal。""",
}

_SIGNAL_PATTERNS: tuple[tuple[LessonSignal, re.Pattern[str]], ...] = (
    (
        "explicit_correction",
        re.compile(r"(?i)(?:更正|纠正|我(?:刚才|之前)?说错了|之前.{0,24}(?:错了|不对)|\bi was wrong\b|\bcorrection\b)"),
    ),
    (
        "high_cost_failure",
        re.compile(
            r"(?i)(?:(?:导致|造成).{0,40}(?:数据损坏|资金损失|安全风险|严重失败)|"
            r"(?:data loss|financial loss|security risk|costly failure))"
        ),
    ),
    (
        "persistent_must_not",
        re.compile(
            r"(?i)(?:(?:以后|今后|从此).{0,80}(?:禁止|不得|绝不能)|"
            r"(?:from now on|always).{0,80}(?:must not|never)|\bnever again\b)"
        ),
    ),
    (
        "persistent_must",
        re.compile(r"(?i)(?:(?:以后|今后|从此).{0,80}(?:必须|务必)|" r"(?:from now on|always).{0,80}\bmust\b)"),
    ),
    (
        "reusable_guardrail",
        re.compile(
            r"(?i)(?:(?:每次|任何时候).{0,80}(?:先|检查|验证)|"
            r"(?:every time|before any).{0,80}(?:check|verify|validate))"
        ),
    ),
)


def classify_lesson_signal(value: str, evidence_quote: str) -> LessonSignal:
    """Classify only an explicit signal present in the grounded evidence quote."""

    del value  # Admission separately proves that this evidence supports the public claim value.
    evidence = unicodedata.normalize("NFKC", evidence_quote).strip()
    if not evidence:
        return "none"
    for signal, pattern in _SIGNAL_PATTERNS:
        if pattern.search(evidence):
            return signal
    return "none"


def lesson_signal_rules_fingerprint() -> dict[str, str]:
    return {signal: pattern.pattern for signal, pattern in _SIGNAL_PATTERNS}


def validate_lesson_signal_mode(mode: LessonSignalMode) -> LessonSignalMode:
    if mode not in {"off", "observe", "enforce"}:
        raise ValueError("lesson_signal_mode must be 'off', 'observe', or 'enforce'")
    return mode


def evaluate_lesson_signal(value: str, evidence_quote: str, mode: LessonSignalMode) -> tuple[LessonSignal, bool]:
    signal = classify_lesson_signal(value, evidence_quote)
    if mode != "off":
        current_audit().emit(
            "extract",
            "lesson_signal_checked",
            "would_promote" if signal != "none" else "unchanged",
            detail={"mode": mode, "signal": signal},
        )
    return signal, mode == "enforce" and signal != "none"


def lesson_notability_prompt(prompt: str, language: Literal["zh", "en"], mode: LessonSignalMode) -> str:
    return f"{prompt}\n\n{_GUIDANCE[language]}" if mode == "enforce" else prompt
