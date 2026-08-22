import pytest

from hl_mem.domain.claims.state_projection import project_state_coordinate


def test_state_projection_preserves_namespace_and_coordinate_qualifiers():
    first = project_state_coordinate(
        namespace="tenant-a",
        subject=" API ",
        canonical_slot="state.service_health",
        qualifiers={"service": " API ", "note": "ignored"},
    )
    second = project_state_coordinate(
        namespace="tenant-b",
        subject="api",
        canonical_slot="state.service_health",
        qualifiers={"service": "api"},
    )

    assert first is not None and second is not None
    assert first.canonical_subject == "api"
    assert first.coordinate_qualifiers == (("service", '"api"'),)
    assert first != second


@pytest.mark.parametrize(
    ("subject", "slot", "qualifiers"),
    [
        ("unknown", "config.version", {}),
        ("api", "state.service_health", {}),
        ("api", "profile.preference", {}),
    ],
)
def test_state_projection_fails_closed_without_a_trusted_state_axis(subject, slot, qualifiers):
    assert (
        project_state_coordinate(namespace="tenant-a", subject=subject, canonical_slot=slot, qualifiers=qualifiers)
        is None
    )
