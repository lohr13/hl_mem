from __future__ import annotations

import pytest

import scripts.run_v029_temporal_replay as replay_module
from scripts.run_v029_temporal_replay import (
    PRICE_CORRECTION_EVENT_IDS,
    TAILSCALE_SNAPSHOT_IDS,
    ReplayGateError,
    enforce_gate,
)


def _report(
    *,
    precision: float = 1.0,
    coverage: float = 1.0,
    false_links: int = 0,
    tailscale_passed: bool = True,
):
    return {
        "gates": {
            "price_precision_actual": precision,
            "price_coverage_actual": coverage,
            "false_coexist_links_actual": false_links,
            "tailscale_sequence_passed": tailscale_passed,
            "passed": precision == 1.0 and coverage == 1.0 and false_links == 0 and tailscale_passed,
        }
    }


def test_replay_manifest_is_the_fixed_14_price_events_and_three_snapshots() -> None:
    assert len(PRICE_CORRECTION_EVENT_IDS) == len(set(PRICE_CORRECTION_EVENT_IDS)) == 14
    assert TAILSCALE_SNAPSHOT_IDS == (
        "d26948807963460590703ee1b4b7c0a3",
        "c844a1a27e2945c5800e499febce41e2",
        "a7f5ea83e2554825adfeec029dbd63b4",
    )


def test_replay_gate_accepts_only_perfect_precision_and_zero_coexistence_links() -> None:
    enforce_gate(_report())

    with pytest.raises(ReplayGateError, match="precision=0.99"):
        enforce_gate(_report(precision=0.99))
    with pytest.raises(ReplayGateError, match="coverage=0.99"):
        enforce_gate(_report(coverage=0.99))
    with pytest.raises(ReplayGateError, match="false_coexist_links=1"):
        enforce_gate(_report(false_links=1))
    with pytest.raises(ReplayGateError, match="tailscale_passed=False"):
        enforce_gate(_report(tailscale_passed=False))


def test_replay_gate_rejects_partial_price_coverage_even_with_perfect_precision(monkeypatch) -> None:
    price_cases = [
        {
            "actual_outcome": "state_change" if index == 0 else "uncertain",
            "expected_outcome": "state_change",
        }
        for index in range(len(PRICE_CORRECTION_EVENT_IDS))
    ]
    monkeypatch.setattr(replay_module, "_price_cases", lambda connection: price_cases)
    monkeypatch.setattr(
        replay_module,
        "_tailscale_cases",
        lambda connection: [{"actual_outcome": "entails", "expected_outcome": "entails"}],
    )
    monkeypatch.setattr(replay_module, "_coexistence_cases", lambda connection: {})

    report = replay_module._evaluate_copy(object())

    assert report["price_corrections"]["precision"] == 1.0
    assert report["price_corrections"]["coverage"] == pytest.approx(1 / 14)
    assert report["gates"]["passed"] is False
    with pytest.raises(ReplayGateError, match="coverage="):
        enforce_gate(report)
