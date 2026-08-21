from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from hl_mem.domain.claims.state_coordinates import StateCoordinate


def test_state_coordinate_constructs_an_immutable_sorted_coordinate() -> None:
    source_qualifiers = {
        "service": "api",
        "deployment": {"zone": "east", "replicas": [1, 2]},
    }

    coordinate = StateCoordinate(
        namespace="default",
        canonical_subject="hl_mem",
        canonical_slot="state.service_health",
        coordinate_qualifiers=source_qualifiers,
    )
    source_qualifiers["service"] = "worker"

    assert coordinate.namespace == "default"
    assert coordinate.canonical_subject == "hl_mem"
    assert coordinate.canonical_slot == "state.service_health"
    assert coordinate.coordinate_qualifiers == (
        ("deployment", '{"replicas":[1,2],"zone":"east"}'),
        ("service", '"api"'),
    )
    with pytest.raises(FrozenInstanceError):
        coordinate.namespace = "other"  # type: ignore[misc]


def test_state_coordinate_equality_and_hash_ignore_qualifier_insertion_order() -> None:
    left = StateCoordinate(
        "default",
        "hl_mem",
        "config.version",
        {"platform": "windows", "component": "server"},
    )
    right = StateCoordinate(
        "default",
        "hl_mem",
        "config.version",
        {"component": "server", "platform": "windows"},
    )

    assert left == right
    assert hash(left) == hash(right)


def test_state_coordinate_equality_preserves_json_type_boundaries() -> None:
    coordinates = {
        StateCoordinate("default", "hl_mem", "config.version", {"value": {"a": 1}}),
        StateCoordinate("default", "hl_mem", "config.version", {"value": [["a", 1]]}),
        StateCoordinate("default", "hl_mem", "config.version", {"value": True}),
        StateCoordinate("default", "hl_mem", "config.version", {"value": 1}),
        StateCoordinate("default", "hl_mem", "config.version", {"value": 1.0}),
    }

    assert len(coordinates) == 5


@pytest.mark.parametrize("field", ["namespace", "canonical_subject", "canonical_slot"])
def test_state_coordinate_rejects_blank_identifiers(field: str) -> None:
    values = {
        "namespace": "default",
        "canonical_subject": "hl_mem",
        "canonical_slot": "config.version",
    }
    values[field] = " \u3000"

    with pytest.raises(ValueError, match=field):
        StateCoordinate(**values)


def test_state_coordinate_rejects_invalid_qualifiers() -> None:
    with pytest.raises(ValueError, match="qualifier key"):
        StateCoordinate("default", "hl_mem", "config.version", {" ": "windows"})
    with pytest.raises(TypeError, match="JSON-compatible"):
        StateCoordinate("default", "hl_mem", "config.version", {"platform": object()})


@pytest.mark.parametrize("qualifiers", [[], [["value", 1]], "", "value", 0, 1, False, True])
def test_state_coordinate_rejects_non_mapping_qualifier_containers(qualifiers: Any) -> None:
    with pytest.raises(TypeError, match="coordinate_qualifiers must be a mapping"):
        StateCoordinate("default", "hl_mem", "config.version", qualifiers)
