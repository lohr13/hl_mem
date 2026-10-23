from __future__ import annotations

import pytest

from hl_mem.domain.provenance import (
    aggregate_event_provenance,
    decide_claim_admission,
    decide_extraction_admission,
    validate_event_provenance,
)


def _event(origin: str = "direct_user", session: str = "interactive", event_type: str = "message") -> dict[str, str]:
    return {"origin_class": origin, "session_kind": session, "event_type": event_type}


@pytest.mark.parametrize("field", ["origin_class", "session_kind"])
def test_event_provenance_rejects_values_outside_closed_contract(field: str) -> None:
    event = _event()
    event[field] = "invented"

    with pytest.raises(ValueError, match=field):
        validate_event_provenance(event)


def test_aggregate_event_provenance_uses_most_conservative_source_and_session() -> None:
    summary = aggregate_event_provenance([_event(), _event("external_derived", "interactive"), _event("agent", "cron")])

    assert summary.origin_class == "external_derived"
    assert summary.session_kind == "cron"
    assert summary.external is True
    assert summary.automated is True


def test_aggregate_event_provenance_preserves_external_signal_when_system_is_stricter() -> None:
    summary = aggregate_event_provenance([_event("external", "interactive"), _event("system", "cron")])

    assert summary.origin_class == "system"
    assert summary.external is True
    assert summary.automated is True


@pytest.mark.parametrize("session", ["heartbeat", "subagent"])
def test_enforce_blocks_automated_extraction_but_observe_keeps_current_flow(session: str) -> None:
    events = [_event("system", session)]

    enforced = decide_extraction_admission(events, mode="enforce")
    observed = decide_extraction_admission(events, mode="observe")

    assert (enforced.allowed, enforced.reason_code) == (False, f"blocked_{session}")
    assert (observed.allowed, observed.reason_code) == (True, f"observe_{session}")


@pytest.mark.parametrize("origin,session", [("external", "interactive"), ("system", "cron")])
def test_external_and_cron_claims_receive_low_temporal_observation_policy(origin: str, session: str) -> None:
    decision = decide_claim_admission(
        [_event(origin, session)],
        assertion_kind="unknown",
        scope="permanent",
        mode="enforce",
    )

    assert decision.allowed is True
    assert decision.source_authority == "low"
    assert decision.assertion_kind == "observation"
    assert decision.scope == "temporal"


def test_inference_is_not_promoted_and_explicit_memory_keeps_retention() -> None:
    decision = decide_claim_admission(
        [_event("external_derived", "interactive", "explicit_memory")],
        assertion_kind="inference",
        scope="permanent",
        mode="enforce",
    )

    assert decision.assertion_kind == "inference"
    assert decision.scope == "permanent"


def test_unknown_legacy_and_observe_mode_do_not_override_existing_claim_semantics() -> None:
    legacy = decide_claim_admission(
        [_event("unknown", "unknown")],
        assertion_kind="unknown",
        scope="permanent",
        mode="enforce",
    )
    observed = decide_claim_admission(
        [_event("external", "cron")],
        assertion_kind="unknown",
        scope="permanent",
        mode="observe",
    )

    assert legacy.preserve_existing is True
    assert observed.preserve_existing is True
    assert (legacy.source_authority, legacy.assertion_kind, legacy.scope) == (None, None, None)
    assert (observed.source_authority, observed.assertion_kind, observed.scope) == (None, None, None)
