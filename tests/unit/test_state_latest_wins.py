from __future__ import annotations

import json
from dataclasses import MISSING, replace
from datetime import datetime, timezone
from inspect import Parameter, signature
from pathlib import Path
from typing import Any

import pytest

from hl_mem.domain.claims.state_coordinates import StateCoordinate
from hl_mem.state_latest_wins import (
    TEMPORAL_RELATIONS,
    CurrentnessProof,
    CurrentTipState,
    VersionClaim,
    parse_version_atom,
    resolve_latest_wins,
)

DATASET_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "datasets"
FROZEN_RELATIONS = {
    "duplicate",
    "corroborates",
    "supersedes_existing",
    "historical_predecessor",
    "compatible",
    "needs_review",
}
BASE_TIP = CurrentTipState(1, "active", True, True)


def _coordinate(
    *, namespace: str = "default", subject: str = "agent:local_pony", slot: str = "config.version", **qualifiers: str
) -> StateCoordinate:
    return StateCoordinate(namespace, subject, slot, qualifiers)


def _claim(
    claim_id: str,
    value: str,
    hour: int | None,
    *,
    coordinate: StateCoordinate | None = None,
    evidence_id: str | None = None,
    authority: int = 3,
    **changes: Any,
) -> VersionClaim:
    event_time = None if hour is None else datetime(2026, 8, 26, hour, tzinfo=timezone.utc)
    values: dict[str, Any] = {
        "claim_id": claim_id,
        "coordinate": coordinate or _coordinate(),
        "value": value,
        "event_time": event_time,
        "source_authority": authority,
        "evidence_id": evidence_id or f"evidence-{claim_id}",
        "event_time_trusted": True,
        "evidence_grounded": True,
    }
    values.update(changes)
    return VersionClaim(**values)


def _proof(incoming: VersionClaim, **changes: Any) -> CurrentnessProof:
    values: dict[str, Any] = {
        "schema_version": "status_report_v1",
        "producer_contract": "hl_mem.report-version-v1",
        "package": "hl_mem",
        "runtime_version": incoming.value,
        "namespace": incoming.coordinate.namespace,
        "canonical_entity_id": incoming.coordinate.canonical_subject,
        "alias_version": 1,
        "observed_at": incoming.event_time,
        "producer_and_owner_verified": True,
    }
    values.update(changes)
    return CurrentnessProof(**values)


def _resolve(
    existing: VersionClaim,
    incoming: VersionClaim,
    *,
    proof: CurrentnessProof | None = None,
    tip: CurrentTipState | None = None,
    aliases: dict[str, str] | None = None,
):
    return resolve_latest_wins(existing, incoming, proof or _proof(incoming), tip or BASE_TIP, version_aliases=aliases)


def test_temporal_relation_enum_is_exactly_the_adr_six() -> None:
    assert TEMPORAL_RELATIONS == FROZEN_RELATIONS


def test_all_permission_bearing_preconditions_must_be_explicit() -> None:
    assert VersionClaim.__dataclass_fields__["event_time_trusted"].default is MISSING
    assert VersionClaim.__dataclass_fields__["evidence_grounded"].default is MISSING
    assert CurrentnessProof.__dataclass_fields__["producer_and_owner_verified"].default is MISSING
    assert all(field.default is MISSING for field in CurrentTipState.__dataclass_fields__.values())
    assert signature(resolve_latest_wins).parameters["tip"].default is Parameter.empty


@pytest.mark.parametrize(
    "value", ["v1.2.3-alpha", "1.2.3+7", "2026.08.26", "abc1234", ">=1.2.3", "version one", "V1.2.3", "01.2.3"]
)
def test_narrow_version_parser_rejects_non_frozen_forms(value: str) -> None:
    assert parse_version_atom(value) is None


def test_narrow_version_parser_accepts_semver_atoms_and_explicit_alias_only() -> None:
    assert parse_version_atom("1.2.3") == (1, 2, 3)
    assert parse_version_atom("v1.2.3") == (1, 2, 3)
    assert parse_version_atom("1.2.3", {"1.2.3": "v9.9.9"}) == (1, 2, 3)
    assert parse_version_atom("release-current") is None
    assert parse_version_atom("release-current", {"release-current": "v1.2.3"}) == (1, 2, 3)
    assert parse_version_atom("Release-Current", {"release-current": "v1.2.3"}) is None


def test_later_event_time_supersedes_even_when_version_number_decreases() -> None:
    existing = _claim("old", "v7.4.0", 10)
    incoming = _claim("rollback", "v7.3.0", 11)

    resolution = _resolve(existing, incoming)

    assert resolution.relation == "supersedes_existing"
    assert (resolution.older_id, resolution.newer_id) == ("old", "rollback")
    assert resolution.event_time_source == "trusted_event_time"


def test_late_arrival_is_historical_predecessor_even_when_recording_is_later() -> None:
    existing = _claim("current", "v2.0.0", 11)
    incoming = _claim("late", "v9.0.0", 10)

    resolution = _resolve(existing, incoming)

    assert resolution.relation == "historical_predecessor"
    assert (resolution.older_id, resolution.newer_id) == ("late", "current")


@pytest.mark.parametrize("same_evidence, expected", [(True, "duplicate"), (False, "corroborates")])
def test_equivalent_version_atom_uses_evidence_identity(same_evidence: bool, expected: str) -> None:
    existing = _claim("old", "v1.2.3", 10, evidence_id="shared")
    incoming = _claim("new", "1.2.3", 11, evidence_id="shared" if same_evidence else "independent")

    assert _resolve(existing, incoming).relation == expected


@pytest.mark.parametrize(
    ("coordinate", "reason"),
    [
        (_coordinate(namespace="other"), "coordinate_mismatch"),
        (_coordinate(subject="agent:other"), "coordinate_mismatch"),
        (_coordinate(slot="state.service_health"), "coordinate_mismatch"),
        (_coordinate(deployment="green"), "coordinate_mismatch"),
    ],
)
def test_exact_coordinate_boundary_is_compatible(coordinate: StateCoordinate, reason: str) -> None:
    existing = _claim("old", "v1.0.0", 10)
    incoming = _claim("new", "v2.0.0", 11, coordinate=coordinate)

    resolution = _resolve(existing, incoming)

    assert resolution.relation == "compatible"
    assert resolution.reason == reason


@pytest.mark.parametrize(
    "mutation",
    [
        {"assertion_kind": "plan"},
        {"assertion_kind": "quotation"},
        {"assertion_kind": "historical"},
        {"polarity": "negative"},
        {"semantic_anchors": frozenset({("anchor", "different")})},
        {"semantic_anchors": frozenset({("unit", "container")})},
        {"semantic_anchors": frozenset({("environment", "staging")})},
        {"semantic_anchors": frozenset({("deployment", "green")})},
        {"semantic_anchors": frozenset({("role", "target")})},
        {"role_count": 2},
        {"payload_count": 2},
        {"atomic_value": False},
        {"evidence_grounded": False},
    ],
)
def test_each_claim_level_hard_veto_fails_closed(mutation: dict[str, Any]) -> None:
    existing = _claim("old", "v1.0.0", 10)
    incoming = replace(_claim("new", "v2.0.0", 11), **mutation)

    assert _resolve(existing, incoming).relation == "needs_review"


@pytest.mark.parametrize(
    "proof_change",
    [
        {"schema_version": "status_report_v2"},
        {"producer_contract": "free-text"},
        {"package": "other"},
        {"runtime_version": "v9.9.9"},
        {"namespace": "other"},
        {"canonical_entity_id": "agent:other"},
        {"alias_version": 0},
        {"observed_at": datetime(2026, 8, 26, 12, tzinfo=timezone.utc)},
        {"producer_and_owner_verified": False},
    ],
)
def test_fixed_currentness_proof_contract_fails_closed(proof_change: dict[str, Any]) -> None:
    existing = _claim("old", "v1.0.0", 10)
    incoming = _claim("new", "v2.0.0", 11)

    assert _resolve(existing, incoming, proof=replace(_proof(incoming), **proof_change)).relation == "needs_review"


@pytest.mark.parametrize(
    "tip",
    [
        replace(BASE_TIP, current_tip_count=2),
        replace(BASE_TIP, old_tip_status="disputed"),
        replace(BASE_TIP, old_tip_status="manual"),
        replace(BASE_TIP, old_tip_status="open_conflict"),
        replace(BASE_TIP, chain_acyclic=False),
        replace(BASE_TIP, local_snapshot_matches=False),
    ],
)
def test_tip_and_local_transaction_preconditions_fail_closed(tip: CurrentTipState) -> None:
    existing = _claim("old", "v1.0.0", 10)
    incoming = _claim("new", "v2.0.0", 11)

    assert _resolve(existing, incoming, tip=tip).relation == "needs_review"


def test_missing_equal_untrusted_time_and_lower_authority_need_review() -> None:
    existing = _claim("old", "v1.0.0", 10)
    variants = [
        _claim("missing", "v2.0.0", None),
        _claim("equal", "v2.0.0", 10),
        _claim("untrusted", "v2.0.0", 11, event_time_trusted=False),
        _claim("lower", "v2.0.0", 11, authority=2),
    ]

    assert [_resolve(existing, item).relation for item in variants] == ["needs_review"] * 4


def test_plain_observation_without_independent_proof_never_supersedes() -> None:
    existing = _claim("old", "v1.0.0", 10)
    incoming = _claim("chat-observation", "v2.0.0", 11)

    assert resolve_latest_wins(existing, incoming, None, BASE_TIP).relation == "needs_review"


def test_missing_event_time_cannot_validate_proof_by_none_equality() -> None:
    existing = _claim("old", "v1.0.0", 10, evidence_id="shared")
    incoming = _claim("retry", "1.0.0", None, evidence_id="shared")

    assert _resolve(existing, incoming).relation == "needs_review"


def test_property_a_to_b_to_a_creates_new_acyclic_single_tip_occurrence() -> None:
    a1 = _claim("a1", "v1.0.0", 9)
    b = _claim("b", "v2.0.0", 10)
    a2 = _claim("a2", "v1.0.0", 11)
    first = _resolve(a1, b)
    second = _resolve(b, a2)
    edges = {(first.older_id, first.newer_id), (second.older_id, second.newer_id)}

    assert first.relation == second.relation == "supersedes_existing"
    assert edges == {("a1", "b"), ("b", "a2")}
    assert a1.claim_id != a2.claim_id
    assert {new for _, new in edges} - {old for old, _ in edges} == {"a2"}
    assert all(old != new for old, new in edges)


def test_property_equivalent_merge_does_not_close_tip_or_increment_revision() -> None:
    valid_to = None
    active_revision_count = 1
    existing = _claim("tip", "v1.0.0", 10, evidence_id="same")
    duplicate = _claim("retry", "1.0.0", 11, evidence_id="same")
    corroborate = _claim("witness", "v1.0.0", 12, evidence_id="other")

    relations = [_resolve(existing, duplicate).relation, _resolve(existing, corroborate).relation]

    assert relations == ["duplicate", "corroborates"]
    assert valid_to is None
    assert active_revision_count == 1


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _calibration_coordinate(raw: dict[str, Any]) -> StateCoordinate:
    return StateCoordinate(
        raw["namespace"], raw["canonical_subject"], raw["canonical_slot"], raw["coordinate_qualifiers"]
    )


def _calibration_claim(raw: dict[str, Any]) -> VersionClaim:
    subtype = str(raw.get("assertion_kind"))
    return VersionClaim(
        claim_id=raw["claim_id"],
        coordinate=_calibration_coordinate(raw["coordinate"]),
        value=raw["value"],
        assertion_kind=subtype,
        event_time=raw["event_time"],
        source_authority=raw["source_authority"],
        evidence_id=raw["source_id"],
        event_time_trusted=True,
        evidence_grounded=True,
        polarity="negative" if subtype == "negation" else "positive",
        role_count=int(raw.get("role_count", 1)),
        payload_count=int(raw.get("payload_count", 1)),
    )


def _calibration_proof(raw: dict[str, Any] | None) -> CurrentnessProof | None:
    if raw is None:
        return None
    return CurrentnessProof(
        schema_version=raw["schema_version"],
        producer_contract=raw["producer_contract"],
        package=raw["package"],
        runtime_version=raw["runtime_version"],
        namespace=raw["namespace"],
        canonical_entity_id=raw["subject_proof"]["canonical_entity_id"],
        alias_version=raw["subject_proof"]["alias_version"],
        observed_at=raw["observed_at"],
        producer_and_owner_verified=True,
    )


def test_frozen_calibration_300_matches_every_expected_relation() -> None:
    corpus = _load_jsonl(DATASET_DIR / "v300_latest_wins_calibration_corpus.jsonl")
    gold = {row["bundle_id"]: row for row in _load_jsonl(DATASET_DIR / "v300_latest_wins_calibration_gold.jsonl")}
    actual: dict[str, str] = {}
    for row in corpus:
        chain = row["chain_state"]
        resolution = resolve_latest_wins(
            _calibration_claim(row["existing_claim"]),
            _calibration_claim(row["incoming_claim"]),
            _calibration_proof(row["currentness_proof"]),
            CurrentTipState(
                current_tip_count=chain["current_tip_count"],
                old_tip_status=chain["old_tip_status"],
                chain_acyclic=chain["acyclic"],
                local_snapshot_matches=True,
            ),
        )
        actual[row["bundle_id"]] = resolution.relation

    assert len(actual) == 300
    assert actual == {bundle_id: row["expected_temporal_relation"] for bundle_id, row in gold.items()}
