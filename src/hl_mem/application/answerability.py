"""召回答案可断言性与拒答类型的统一语义。"""

from __future__ import annotations

from typing import Literal, TypeGuard

Answerability = Literal["supported", "low_confidence", "no_evidence"]
AbstentionKind = Literal["none", "hard", "soft"]

ANSWERABILITY_VALUES = frozenset({"supported", "low_confidence", "no_evidence"})


def is_answerability(value: object) -> TypeGuard[Answerability]:
    """判断外部值是否属于冻结的 answerability 枚举。"""
    return isinstance(value, str) and value in ANSWERABILITY_VALUES


def abstention_kind(answerability: str) -> AbstentionKind:
    """把产品信号映射为 reader 与评测共用的 hard/soft 拒答。"""
    if answerability == "no_evidence":
        return "hard"
    if answerability == "low_confidence":
        return "soft"
    if answerability == "supported":
        return "none"
    raise ValueError(f"unsupported answerability: {answerability!r}")


def is_abstention(answerability: str) -> bool:
    """hard 与 soft 都禁止 reader 作确定性断言。"""
    return abstention_kind(answerability) != "none"
