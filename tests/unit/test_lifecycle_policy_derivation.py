"""Policy 与 Derivation 生命周期守卫测试。"""

from __future__ import annotations

import pytest

from hl_mem.lifecycle import (
    DERIVATION_TRANSITIONS,
    POLICY_TRANSITIONS,
    ClaimStatus,
    DerivationStatus,
    InvalidTransitionError,
    PolicyStatus,
    assert_transition,
    assert_valid_derivation_transition,
    assert_valid_policy_transition,
)


def test_policy_transition_matrix_accepts_only_declared_edges() -> None:
    """Policy 合法边均可通过，retired 保持终态。"""
    for source, target in POLICY_TRANSITIONS:
        assert_valid_policy_transition(source, target)
    with pytest.raises(InvalidTransitionError):
        assert_valid_policy_transition(PolicyStatus.RETIRED, PolicyStatus.ACTIVE)


def test_derivation_transition_matrix_accepts_only_declared_edges() -> None:
    """Derivation 合法边均可通过，stale 不可直接归档。"""
    for source, target in DERIVATION_TRANSITIONS:
        assert_valid_derivation_transition(source, target)
    with pytest.raises(InvalidTransitionError):
        assert_valid_derivation_transition(DerivationStatus.STALE, DerivationStatus.ARCHIVED)


def test_disputed_claim_can_reach_superseded_terminal_state() -> None:
    assert_transition(ClaimStatus.DISPUTED, ClaimStatus.SUPERSEDED)
