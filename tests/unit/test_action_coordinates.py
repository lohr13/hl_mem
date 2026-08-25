from __future__ import annotations

from decimal import Decimal

from hl_mem.domain.action_coordinates import (
    PlanCoordinate,
    QuantityCoordinate,
    coordinate_from_claim,
    project_action_qualifiers,
)


def test_projects_exact_plan_quantity_without_float_rounding() -> None:
    qualifiers = project_action_qualifiers(
        "计划在黄金账户买入 10500.00 份黄金ETF",
        {"account": "黄金账户"},
        is_plan=True,
    )

    assert qualifiers == {
        "account": "黄金账户",
        "action_family": "open",
        "assertion_phase": "plan",
        "direction": "long",
        "quantity": "10500",
        "quantity_mode": "exact",
        "quantity_unit": "share",
    }


def test_projects_all_quantity_for_completed_close() -> None:
    qualifiers = project_action_qualifiers("已全部清仓创新药ETF", {}, is_plan=False)

    assert qualifiers == {
        "action_family": "close",
        "assertion_phase": "execution",
        "direction": "out",
        "quantity_mode": "all",
    }


def test_projects_cancel_and_replacement_phases() -> None:
    cancelled = project_action_qualifiers("取消买入 100 股贵州茅台的计划", {}, is_plan=False)
    replaced = project_action_qualifiers("原计划改为买入 200 股贵州茅台", {}, is_plan=False)

    assert cancelled["assertion_phase"] == "cancellation"
    assert replaced["assertion_phase"] == "replacement"


def test_projects_direct_english_replace_as_replacement() -> None:
    replaced = project_action_qualifiers("replace buy 200 shares NASDAQ:T01", {}, is_plan=False)

    assert replaced["action_family"] == "open"
    assert replaced["assertion_phase"] == "replacement"


def test_coordinate_requires_complete_protected_fields() -> None:
    claim = {
        "namespace_key": "default",
        "canonical_target_entity_id": "instrument:CN:SH:600519",
        "valid_from": "2026-08-25T09:30:00+00:00",
        "occurred_start": None,
        "occurred_end": None,
        "qualifiers": {
            "action_family": "open",
            "assertion_phase": "plan",
            "direction": "long",
            "quantity": "10500.00",
            "quantity_mode": "exact",
            "quantity_unit": "share",
        },
    }

    coordinate = coordinate_from_claim(claim)

    assert coordinate == PlanCoordinate(
        namespace="default",
        canonical_target_entity_id="instrument:CN:SH:600519",
        action_family="open",
        direction="long",
        quantity=QuantityCoordinate("exact", Decimal("10500"), "share"),
        account=None,
        window_start="2026-08-25T09:30:00+00:00",
        window_end=None,
        assertion_phase="plan",
    )


def test_coordinate_rejects_negative_or_missing_unit_quantity() -> None:
    base = {
        "namespace_key": "default",
        "canonical_target_entity_id": "instrument:CN:SH:600519",
        "valid_from": "2026-08-25T09:30:00+00:00",
        "qualifiers": {
            "action_family": "open",
            "assertion_phase": "execution",
            "direction": "long",
            "quantity_mode": "exact",
        },
    }

    assert coordinate_from_claim({**base, "qualifiers": {**base["qualifiers"], "quantity": "-1"}}) is None
    assert coordinate_from_claim({**base, "qualifiers": {**base["qualifiers"], "quantity": "10"}}) is None
