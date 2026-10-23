"""Measurement identity must be proved before temporal quarantine or replacement."""

import pytest

from hl_mem.domain.claims.temporal_links import evaluate_temporal_link
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from tests.unit.test_temporal_linking import _claim, _extracted, _store


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("My annual gross income is $92000", "I earned $135 selling plants on June 2"),
        ("My climbing helmet cost $55", "I sell seedlings for $6.25 each"),
        ("I paid $55 for a helmet", "The old price $55 is replaced by $70 for a backpack"),
    ],
)
def test_unidentified_money_pairs_remain_active_with_evidence(tmp_path, left, right):
    with Database(tmp_path / "memory.db").connect() as connection:
        first = _store(connection, _extracted(left, subject="user"), "event-left", "2026-06-01T00:00:00Z")
        second = _store(connection, _extracted(right, subject="user"), "event-right", "2026-06-02T00:00:00Z")
        repo = ClaimRepository(connection)
        assert repo.get_claim(first.claim_id)["status"] == "active"
        assert repo.get_claim(second.claim_id)["status"] == "active"
        assert first.claim_id != second.claim_id
        assert connection.execute("SELECT count(*) FROM conflict_cases").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Annual income $92000", "Income from a sale $135"),
        ("Price $12 each", "Total cost $120"),
        ("Annual income for 2024 is $92000", "Annual income for 2025 is $97000"),
        ("Revenue from a sale $135", "Revenue from a sale $150"),
    ],
)
def test_shared_owner_or_target_does_not_prove_same_measurement(left, right):
    old = _claim(left, claim_id="left", valid_from="2026-06-01T00:00:00Z")
    new = _claim(right, claim_id="right", valid_from="2026-06-02T00:00:00Z", assertion_kind="observation")
    old["canonical_target_entity_id"] = new["canonical_target_entity_id"] = "org:shop"
    assert evaluate_temporal_link(old, new).outcome in {"distinct_series", "unproven"}


def test_missing_identity_does_not_poison_proven_snapshot_pair():
    from hl_mem.application._ingest_resolution import _resolve_temporal_candidates

    old = _claim("Price $12", claim_id="old", valid_from="2026-06-01T00:00:00Z")
    new = _claim("Price $14", claim_id="new", valid_from="2026-06-02T00:00:00Z", assertion_kind="observation")
    old["canonical_target_entity_id"] = new["canonical_target_entity_id"] = "product:widget"
    unknown = {**old, "id": "unidentified", "canonical_target_entity_id": None, "subject_entity_id": "user"}
    resolution = _resolve_temporal_candidates([unknown, old], new)
    assert resolution.outcome == "snapshot_advance"
    assert [member["id"] for member in resolution.members] == ["old"]
    assert any(item[1] == "unproven" for item in resolution.member_outcomes)


def test_typed_user_is_not_a_measured_product():
    old = _claim("Helmet price $55", claim_id="left", valid_from="2026-06-01T00:00:00Z")
    new = _claim(
        "Seedlings price $6", claim_id="right", valid_from="2026-06-02T00:00:00Z", assertion_kind="observation"
    )
    old["subject_entity_id"] = new["subject_entity_id"] = "person:user"
    assert evaluate_temporal_link(old, new).outcome == "unproven"


@pytest.mark.parametrize("owner", ["person:alice", "person:user", "org:shop"])
@pytest.mark.parametrize("target", [False, True])
def test_named_owner_is_not_a_measured_product(owner, target):
    old = _claim("Helmet cost $55", claim_id="left", valid_from="2026-06-01T00:00:00Z")
    new = _claim(
        "Seedlings price $6", claim_id="right", valid_from="2026-06-02T00:00:00Z", assertion_kind="observation"
    )
    field = "canonical_target_entity_id" if target else "subject_entity_id"
    old[field] = new[field] = owner
    assert evaluate_temporal_link(old, new).outcome == "unproven"


def test_monthly_subscription_price_is_a_rate_not_accumulated_income():
    old = _claim("The monthly price is $12", claim_id="left", valid_from="2026-06-01T00:00:00Z")
    new = _claim(
        "The monthly price is $14", claim_id="right", valid_from="2026-06-02T00:00:00Z", assertion_kind="observation"
    )
    old["canonical_target_entity_id"] = new["canonical_target_entity_id"] = "product:subscription"
    assert evaluate_temporal_link(old, new).outcome == "snapshot_advance"


@pytest.mark.parametrize("target", [False, True])
def test_explicit_measurement_object_and_period_prove_owner_update(target):
    qualifiers = {"measurement_object": "salary", "measurement_period": "2025"}
    old = _claim("Annual salary $90000", claim_id="old", valid_from="2026-06-01T00:00:00Z", qualifiers=qualifiers)
    new = _claim(
        "Annual salary $95000",
        claim_id="new",
        valid_from="2026-06-02T00:00:00Z",
        assertion_kind="observation",
        qualifiers=qualifiers,
    )
    field = "canonical_target_entity_id" if target else "subject_entity_id"
    old[field] = new[field] = "person:user"
    assert evaluate_temporal_link(old, new).outcome == "snapshot_advance"
