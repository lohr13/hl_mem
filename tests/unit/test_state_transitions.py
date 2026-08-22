from __future__ import annotations

from typing import Any

import pytest

from hl_mem.domain.claims.state_transitions import resolve_state_transition

OLD = "2026-08-20T08:00:00+00:00"
NEW = "2026-08-21T08:00:00+00:00"


def _claim(value: str, valid_from: str, **overrides: Any) -> dict[str, Any]:
    claim = {
        "namespace_key": "default",
        "subject_entity_id": "gateway-1",
        "canonical_slot": "config.version",
        "qualifiers": {},
        "assertion_kind": "observation",
        "value": value,
        "valid_from": valid_from,
    }
    claim.update(overrides)
    return claim


@pytest.mark.parametrize(
    ("slot", "qualifiers", "old_value", "new_value"),
    [
        ("config.version", {}, "gateway-1 runs v1.0", "gateway-1 runs v2.0"),
        ("state.service_health", {"service": "api"}, "api healthy", "api unhealthy"),
        ("state.process", {"process": "worker"}, "worker running", "worker stopped"),
        ("state.deployment", {"deployment": "blue"}, "blue ready", "blue failed"),
        ("state.connectivity", {"instance": "node-1"}, "node-1 online", "node-1 offline"),
        ("state.job", {"job": "backup"}, "backup running", "backup complete"),
    ],
)
def test_strictly_ordered_current_state_change_is_actionable(
    slot: str, qualifiers: dict[str, str], old_value: str, new_value: str
) -> None:
    decision = resolve_state_transition(
        _claim(old_value, OLD, canonical_slot=slot, qualifiers=qualifiers),
        _claim(new_value, NEW, canonical_slot=slot, qualifiers=qualifiers),
    )

    assert (decision.outcome, decision.rule_id, decision.snapshot_order) == (
        "snapshot_advance",
        "state-v1:coordinate",
        "newer",
    )


def test_delayed_older_observation_preserves_the_current_tip() -> None:
    decision = resolve_state_transition(_claim("v2", NEW), _claim("v1", OLD))
    assert (decision.outcome, decision.snapshot_order, decision.rationale) == (
        "snapshot_advance",
        "older",
        "older_state_observation",
    )


def test_same_canonical_version_entails_without_a_self_edge() -> None:
    decision = resolve_state_transition(
        _claim("gateway-1 version v1.0", OLD),
        _claim("gateway-1 is running version 1.0", NEW),
    )

    assert (decision.outcome, decision.rationale) == ("entails", "same_state_value")


def test_ambiguous_coordinate_group_routes_to_review() -> None:
    existing = {**_claim("v1", OLD), "_state_group_ambiguous": True}
    decision = resolve_state_transition(existing, _claim("v2", NEW))
    assert (decision.outcome, decision.rationale) == ("uncertain", "state_group_ambiguous")


@pytest.mark.parametrize(
    ("existing", "new", "rationale"),
    [
        (_claim("v1", OLD), _claim("v2", OLD), "state_time_not_strictly_ordered"),
        (_claim("v1", "bad"), _claim("v2", NEW), "state_time_invalid"),
        (_claim("v1", OLD), _claim("v2", NEW, namespace_key="other"), "state_coordinate_differs"),
        (
            _claim("healthy", OLD, canonical_slot="state.service_health", qualifiers={"service": "api"}),
            _claim("unhealthy", NEW, canonical_slot="state.service_health", qualifiers={"service": "web"}),
            "state_coordinate_differs",
        ),
        (_claim("v1", OLD), _claim("v2", NEW, assertion_kind="inference"), "state_not_current_observation"),
        (
            _claim("v1", OLD),
            _claim("v2", NEW, qualifiers={"_state_context": "historical"}),
            "state_not_current_observation",
        ),
    ],
)
def test_unsafe_or_cross_coordinate_updates_fail_closed(
    existing: dict[str, Any], new: dict[str, Any], rationale: str
) -> None:
    decision = resolve_state_transition(existing, new)
    assert decision.outcome in {"not_applicable", "uncertain"}
    assert decision.rationale == rationale
