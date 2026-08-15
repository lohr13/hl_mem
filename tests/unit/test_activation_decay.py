"""Three-arm decay model and activation migration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from hl_mem.ingest.embedder import pack_vector
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.decay import activation_at, decay_claims

NOW = "2026-08-15T00:00:00+00:00"
BASE_ARGS = {
    "temporal_decay_days": 90,
    "temporal_archive_days": 180,
    "permanent_decay_days": 180,
    "permanent_archive_days": 365,
    "access_bonus_every": 10,
    "access_bonus_days": 30,
    "access_bonus_cap_days": 365,
    "rollout_grace_days": 7,
    "min_confidence": 0.05,
    "feedback_lifecycle_mode": "observe",
    "feedback_bonus_cap_days": 180,
    "temporal_half_life_days": 45,
    "permanent_half_life_days": 90,
    "identity_half_life_days": 365,
    "halflife_archive_threshold": 0.05,
    "halflife_archive_grace_days": 7,
}


def _connection(tmp_path, name: str = "activation.db"):
    return Database(tmp_path / name).open()


def _claim(connection, *, claim_id: str = "claim-1", days_old: int = 45, **overrides: object) -> None:
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=days_old)).isoformat()
    claim = {
        "id": claim_id,
        "subject_entity_id": "团队",
        "predicate": "采用",
        "value": "海风看板",
        "recorded_from": recorded,
        "last_accessed_at": recorded,
        "status": "active",
        "scope": "temporal",
        "confidence": 0.8,
        "embedding_dense": pack_vector([1.0]),
    }
    claim.update(overrides)
    assert ClaimRepository(connection).insert_claim(claim)


def _run(connection, model: str, now: str = NOW) -> dict[str, int]:
    return decay_claims(connection, now, decay_model=model, **BASE_ARGS)


def test_migration_042_adds_inert_activation_state(tmp_path) -> None:
    connection = _connection(tmp_path)
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(claims)")}

    assert {"activation_base", "activation", "decay_below_since"} <= columns
    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='042_activation_decay'").fetchone()


def test_activation_formula_is_exponential_and_bounded() -> None:
    assert activation_at(1.0, inactive_days=0, half_life_days=45) == 1.0
    assert activation_at(1.0, inactive_days=45, half_life_days=45) == pytest.approx(0.5)
    assert activation_at(0.8, inactive_days=90, half_life_days=45) == pytest.approx(0.2)


def test_activation_arm_halves_activation_without_changing_confidence(tmp_path) -> None:
    connection = _connection(tmp_path)
    _claim(connection)

    assert _run(connection, "activation_halflife") == {"decayed": 1, "archived": 0}
    row = connection.execute("SELECT confidence,activation,status FROM claims").fetchone()
    assert tuple(row) == pytest.approx((0.8, 0.5, "active"))


def test_activation_hit_only_resets_last_access_then_formula_recovers(tmp_path) -> None:
    connection = _connection(tmp_path)
    _claim(connection)
    _run(connection, "activation_halflife")
    ClaimRepository(connection).record_access(["claim-1"], NOW)

    next_day = "2026-08-16T00:00:00+00:00"
    _run(connection, "activation_halflife", next_day)
    activation, confidence, last_accessed_at = connection.execute(
        "SELECT activation,confidence,last_accessed_at FROM claims"
    ).fetchone()
    assert activation == pytest.approx(2 ** (-1 / 45))
    assert confidence == 0.8
    assert last_accessed_at == NOW


def test_activation_archive_requires_threshold_grace_and_is_idempotent(tmp_path) -> None:
    connection = _connection(tmp_path)
    _claim(connection, days_old=210)

    first = _run(connection, "activation_halflife")
    second = _run(connection, "activation_halflife")

    assert first == {"decayed": 0, "archived": 1}
    assert second == {"decayed": 0, "archived": 0}
    status, embedding, below_since = connection.execute(
        "SELECT status,embedding_dense,decay_below_since FROM claims"
    ).fetchone()
    assert status == "archived"
    assert embedding is None
    assert below_since is not None


def test_confidence_halflife_arm_halves_confidence_without_changing_activation(tmp_path) -> None:
    connection = _connection(tmp_path)
    _claim(connection)

    assert _run(connection, "confidence_halflife") == {"decayed": 1, "archived": 0}
    confidence, activation = connection.execute("SELECT confidence,activation FROM claims").fetchone()
    assert confidence == pytest.approx(0.4)
    assert activation == 1.0


def test_legacy_arm_preserves_existing_linear_behavior(tmp_path) -> None:
    connection = _connection(tmp_path)
    _claim(connection, days_old=100, confidence=0.08)

    assert _run(connection, "legacy_linear") == {"decayed": 1, "archived": 0}
    confidence, activation = connection.execute("SELECT confidence,activation FROM claims").fetchone()
    assert confidence == pytest.approx(0.05)
    assert activation == 1.0


def test_decay_model_defaults_legacy_and_validates_all_three_arms() -> None:
    settings = Settings()
    assert settings.decay_model == "legacy_linear"
    for model in ("legacy_linear", "activation_halflife", "confidence_halflife"):
        replace(Settings.for_test(), decay_model=model).validate()
    with pytest.raises(Exception, match="decay.model"):
        replace(Settings.for_test(), decay_model="invalid").validate()
