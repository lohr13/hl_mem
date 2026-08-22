from hl_mem.domain.claims.attributes import SLOT_REGISTRY, SlotDefinition
from hl_mem.domain.claims.conflicts import coordinate_qualifier_key, slot_qualifier_key


def test_slot_definition_keeps_required_and_coordinate_qualifiers_independent() -> None:
    definition = SlotDefinition(
        name="state.example",
        predicate="状态",
        description="example",
        required_qualifiers=("evidence",),
        coordinate_qualifiers=("service",),
    )

    assert definition.required_qualifiers == ("evidence",)
    assert definition.coordinate_qualifiers == ("service",)


def test_registered_coordinate_qualifiers_preserve_required_projection() -> None:
    assert SLOT_REGISTRY["state.service_health"].required_qualifiers == ("service",)
    assert SLOT_REGISTRY["state.service_health"].coordinate_qualifiers == (
        "service",
        "environment",
        "deployment",
        "instance",
    )
    assert SLOT_REGISTRY["config.version"].coordinate_qualifiers == (
        "component",
        "service",
        "environment",
        "deployment",
        "instance",
        "platform",
    )
    assert SLOT_REGISTRY["preference.ui_theme"].required_qualifiers == ()
    assert SLOT_REGISTRY["preference.ui_theme"].coordinate_qualifiers == ()


def test_coordinate_qualifier_key_preserves_legacy_slot_projection() -> None:
    qualifiers = {"environment": "production", "service": " API "}

    expected = {"service": "api", "environment": "production"}
    assert coordinate_qualifier_key("state.service_health", qualifiers) == expected
    assert slot_qualifier_key("state.service_health", qualifiers) == expected
