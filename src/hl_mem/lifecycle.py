"""Claim 生命周期状态转换守卫。"""

from __future__ import annotations

from enum import Enum


class ClaimStatus(str, Enum):
    """Claim 的生命周期状态。"""

    ACTIVE = "active"
    DISPUTED = "disputed"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"


class InvalidTransitionError(ValueError):
    """Claim 状态转换不在允许矩阵中。"""


ALLOWED_TRANSITIONS: frozenset[tuple[ClaimStatus, ClaimStatus]] = frozenset(
    {
        (ClaimStatus.ACTIVE, ClaimStatus.DISPUTED),
        (ClaimStatus.ACTIVE, ClaimStatus.EXPIRED),
        (ClaimStatus.ACTIVE, ClaimStatus.ARCHIVED),
        (ClaimStatus.ACTIVE, ClaimStatus.SUPERSEDED),
        (ClaimStatus.DISPUTED, ClaimStatus.ARCHIVED),
        (ClaimStatus.DISPUTED, ClaimStatus.EXPIRED),
        (ClaimStatus.DISPUTED, ClaimStatus.ACTIVE),
    }
)


def assert_transition(from_status: str, to_status: str) -> None:
    """断言状态转换合法，非法时抛出 InvalidTransitionError。"""
    try:
        transition = (ClaimStatus(from_status), ClaimStatus(to_status))
    except ValueError as error:
        raise InvalidTransitionError(f"invalid claim status transition: {from_status} -> {to_status}") from error
    if transition not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(f"invalid claim status transition: {from_status} -> {to_status}")
