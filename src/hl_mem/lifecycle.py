"""领域状态机。定义 ClaimStatus 和 EpisodeStatus 枚举、合法转换矩阵和守卫函数。"""

from __future__ import annotations

from enum import Enum

from hl_mem.errors import ConflictError


class ClaimStatus(str, Enum):
    """Claim 的生命周期状态。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISPUTED = "disputed"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class InvalidTransitionError(ConflictError, ValueError):
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
        (ClaimStatus.DISPUTED, ClaimStatus.SUPERSEDED),
        (ClaimStatus.DISPUTED, ClaimStatus.RETRACTED),
        (ClaimStatus.ARCHIVED, ClaimStatus.ACTIVE),
    }
)


class EpisodeStatus(str, Enum):
    """Episode 的生命周期状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PolicyStatus(str, Enum):
    """归纳策略的生命周期状态。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    RETIRED = "retired"


class DerivationStatus(str, Enum):
    """派生记忆的生命周期状态。"""

    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


POLICY_TRANSITIONS: frozenset[tuple[PolicyStatus, PolicyStatus]] = frozenset(
    {
        (PolicyStatus.CANDIDATE, PolicyStatus.ACTIVE),
        (PolicyStatus.CANDIDATE, PolicyStatus.RETIRED),
        (PolicyStatus.ACTIVE, PolicyStatus.RETIRED),
        (PolicyStatus.ACTIVE, PolicyStatus.CANDIDATE),
    }
)


DERIVATION_TRANSITIONS: frozenset[tuple[DerivationStatus, DerivationStatus]] = frozenset(
    {
        (DerivationStatus.ACTIVE, DerivationStatus.STALE),
        (DerivationStatus.STALE, DerivationStatus.ACTIVE),
        (DerivationStatus.ACTIVE, DerivationStatus.ARCHIVED),
        (DerivationStatus.ARCHIVED, DerivationStatus.ACTIVE),
    }
)


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
        raise InvalidTransitionError(f"invalid episode status transition: {from_status} -> {to_status}") from error
    if transition not in ALLOWED_EPISODE_TRANSITIONS:
        raise InvalidTransitionError(f"invalid episode status transition: {from_status} -> {to_status}")


def assert_valid_policy_transition(from_status: str, to_status: str) -> None:
    """断言 Policy 状态转换合法。"""
    try:
        transition = (PolicyStatus(from_status), PolicyStatus(to_status))
    except ValueError as error:
        raise InvalidTransitionError(f"invalid policy status transition: {from_status} -> {to_status}") from error
    if transition not in POLICY_TRANSITIONS:
        raise InvalidTransitionError(f"invalid policy status transition: {from_status} -> {to_status}")


def assert_valid_derivation_transition(from_status: str, to_status: str) -> None:
    """断言 Derivation 状态转换合法。"""
    try:
        transition = (DerivationStatus(from_status), DerivationStatus(to_status))
    except ValueError as error:
        raise InvalidTransitionError(f"invalid derivation status transition: {from_status} -> {to_status}") from error
    if transition not in DERIVATION_TRANSITIONS:
        raise InvalidTransitionError(f"invalid derivation status transition: {from_status} -> {to_status}")
