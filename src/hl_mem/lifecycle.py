"""领域状态机。定义 ClaimStatus 和 EpisodeStatus 枚举、合法转换矩阵和守卫函数。"""

from __future__ import annotations

from enum import Enum


class ClaimStatus(str, Enum):
    """Claim 的生命周期状态。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISPUTED = "disputed"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class InvalidTransitionError(ValueError):
    """Claim 状态转换不在允许矩阵中。"""


ALLOWED_TRANSITIONS: frozenset[tuple[ClaimStatus, ClaimStatus]] = frozenset(
    {
        (ClaimStatus.CANDIDATE, ClaimStatus.ACTIVE),
        (ClaimStatus.CANDIDATE, ClaimStatus.DISPUTED),
        (ClaimStatus.CANDIDATE, ClaimStatus.EXPIRED),
        (ClaimStatus.CANDIDATE, ClaimStatus.ARCHIVED),
        (ClaimStatus.CANDIDATE, ClaimStatus.RETRACTED),
        (ClaimStatus.ACTIVE, ClaimStatus.DISPUTED),
        (ClaimStatus.ACTIVE, ClaimStatus.EXPIRED),
        (ClaimStatus.ACTIVE, ClaimStatus.ARCHIVED),
        (ClaimStatus.ACTIVE, ClaimStatus.SUPERSEDED),
        (ClaimStatus.ACTIVE, ClaimStatus.RETRACTED),
        (ClaimStatus.DISPUTED, ClaimStatus.ARCHIVED),
        (ClaimStatus.DISPUTED, ClaimStatus.EXPIRED),
        (ClaimStatus.DISPUTED, ClaimStatus.ACTIVE),
        (ClaimStatus.DISPUTED, ClaimStatus.RETRACTED),
    }
)


class EpisodeStatus(str, Enum):
    """Episode 的生命周期状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_EPISODE_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {
        EpisodeStatus.SUCCESS,
        EpisodeStatus.FAILED,
        EpisodeStatus.CANCELLED,
    }
)


ALLOWED_EPISODE_TRANSITIONS: frozenset[tuple[EpisodeStatus, EpisodeStatus]] = frozenset(
    {
        (EpisodeStatus.RUNNING, EpisodeStatus.SUCCESS),
        (EpisodeStatus.RUNNING, EpisodeStatus.FAILED),
        (EpisodeStatus.RUNNING, EpisodeStatus.CANCELLED),
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


def assert_episode_transition(from_status: str, to_status: str) -> None:
    """断言 Episode 状态转换合法。"""
    try:
        transition = (EpisodeStatus(from_status), EpisodeStatus(to_status))
    except ValueError as error:
        raise InvalidTransitionError(
            f"invalid episode status transition: {from_status} -> {to_status}"
        ) from error
    if transition not in ALLOWED_EPISODE_TRANSITIONS:
        raise InvalidTransitionError(f"invalid episode status transition: {from_status} -> {to_status}")
