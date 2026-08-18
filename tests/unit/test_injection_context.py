from __future__ import annotations

import pytest

from hl_mem.recall.injection import InjectionContext


def test_injection_context_normalizes_policy_versions_and_freshness_hour_bucket() -> None:
    """Catches cache/trace contexts that depend on mapping order or sub-hour wall-clock drift."""
    context = InjectionContext.create(
        delivery_purpose="passive_injection",
        experiment_variant="E1F1",
        echo_variant="observe",
        freshness_variant="observe",
        policy_versions={"freshness": "risk-age-v1", "echo": "same-session-v1"},
        rendering_now="2026-08-18T12:34:56+08:00",
    )

    assert context.policy_versions == (("echo", "same-session-v1"), ("freshness", "risk-age-v1"))
    assert context.freshness_time_bucket == "2026-08-18T04:00:00+00:00"
    assert context.envelope() == {
        "schema_version": "injection-v1",
        "delivery_purpose": "passive_injection",
        "experiment_variant": "E1F1",
        "policy_versions": {"echo": "same-session-v1", "freshness": "risk-age-v1"},
        "variants": {"echo": "observe", "freshness": "observe"},
        "rendering_now": "2026-08-18T04:34:56+00:00",
        "freshness_time_bucket": "2026-08-18T04:00:00+00:00",
    }


def test_injection_context_rejects_naive_rendering_time() -> None:
    """Catches a replay clock whose timezone would make age labels host-dependent."""
    with pytest.raises(ValueError, match="timezone-aware"):
        InjectionContext.create(rendering_now="2026-08-18T12:00:00")
