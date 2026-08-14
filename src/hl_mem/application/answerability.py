"""召回答案置信信号与评测拒答类型的统一语义。"""

from __future__ import annotations

from typing import Literal, TypeGuard

Answerability = Literal["supported", "low_confidence", "no_evidence"]
AbstentionKind = Literal["none", "hard", "soft"]

ANSWERABILITY_VALUES = frozenset({"supported", "low_confidence", "no_evidence"})


def is_answerability(value: object) -> TypeGuard[Answerability]:
    """判断外部值是否属于冻结的 answerability 枚举。"""
    return isinstance(value, str) and value in ANSWERABILITY_VALUES


def abstention_kind(answerability: str) -> AbstentionKind:
    """把产品信号映射为 API 元数据与评测共用的 hard/soft 分类。"""
    if answerability == "no_evidence":
        return "hard"
    if answerability == "low_confidence":
        return "soft"
    if answerability == "supported":
        return "none"
    raise ValueError(f"unsupported answerability: {answerability!r}")


def is_abstention(answerability: str) -> bool:
    """判断信号是否计入评测的 hard/soft 拒答并集；不决定 reader 控制流。"""
    return abstention_kind(answerability) != "none"
