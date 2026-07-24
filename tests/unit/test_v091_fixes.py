"""v0.9.1 审查问题的直接回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from hl_mem.application.recall import RecallService
from hl_mem.domain.claims.attributes import SLOT_REGISTRY, validate_slot_instance
from hl_mem.domain.claims.retention import TTLPolicy, compute_expiration
from hl_mem.errors import ConfigurationError
from hl_mem.ingest.embedder import FakeEmbedder
from hl_mem.recall.staged_pipeline import _is_preference_claim
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.migrations.backfill_claim_slots_v1 import backfill_claim_slots_v1
from hl_mem.storage.migrations.backfill_conflict_key_v3 import (
    DATA_MIGRATION_VERSION,
    backfill_conflict_keys_v3,
)
from hl_mem.workers.backfill_expires_at import backfill_expires_at
from hl_mem.workers.deduplicate import _apply_equivalent_pair
from hl_mem.workers.ttl import expire_claims


@pytest.mark.parametrize(
    "slot",
    [name for name, definition in SLOT_REGISTRY.items() if definition.is_operational and definition.required_qualifiers],
)
def test_required_slot_qualifier_rejects_missing_and_empty_values(slot: str) -> None:
    required = SLOT_REGISTRY[slot].required_qualifiers
    valid = {key: f" {key.upper()} " for key in required}
    assert validate_slot_instance(slot.upper(), valid) == slot
    assert validate_slot_instance(slot, {}) is None
    assert validate_slot_instance(slot, {key: "　" for key in required}) is None


def test_compute_expiration_normalizes_positive_and_negative_offsets_to_utc() -> None:
    policy = TTLPolicy(temporal_ttl_days_low=1)
    positive, _ = compute_expiration(
        "temporal", 0.1, "stable", None, None, "2026-01-02T01:00:00+08:00", "", policy
    )
    negative, _ = compute_expiration(
        "temporal", 0.1, "stable", None, None, "2026-01-01T12:00:00-05:00", "", policy
    )
    assert positive == "2026-01-02T17:00:00+00:00"
    assert negative == "2026-01-02T17:00:00+00:00"


def test_ttl_worker_compares_mixed_offset_storage_as_instants(tmp_path) -> None:
    connection = Database(tmp_path / "mixed-offset.db").open()
    ClaimRepository(connection).insert_claim(
        {
            "id": "offset",
            "namespace_key": "default",
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "status": "active",
            "expires_at": "2026-01-02T01:00:00+08:00",
        }
    )
    assert expire_claims(connection, "2026-01-01T18:00:00Z") == {"expired": 1}


def test_slot_backfill_preserves_existing_classification_by_default(tmp_path) -> None:
    connection = Database(tmp_path / "slot-backfill.db").open()
    ClaimRepository(connection).insert_claim(
        {
            "id": "existing",
            "namespace_key": "default",
            "subject_entity_id": "user",
            "predicate": "偏好",
            "canonical_attribute": "preference.ui_theme",
            "canonical_slot": "preference.response_style",
            "topic_tags_json": '["preference"]',
            "qualifiers": {},
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "status": "active",
        }
    )
    stats = backfill_claim_slots_v1(connection, apply=True)
    claim = ClaimRepository(connection).get_claim("existing")
    assert stats.attempted == 0
    assert claim is not None and claim["canonical_slot"] == "preference.response_style"


def test_expires_backfill_scope_cas_does_not_overwrite_permanent_claim(tmp_path) -> None:
    connection = Database(tmp_path / "expires-backfill.db").open()
    ClaimRepository(connection).insert_claim(
        {
            "id": "claim",
            "namespace_key": "default",
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "status": "active",
            "scope": "temporal",
            "importance": 0.5,
        }
    )
    connection.execute("UPDATE claims SET scope='permanent' WHERE id='claim'")
    connection.commit()
    result = backfill_expires_at(
        connection,
        TTLPolicy(),
        dry_run=False,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert result["scanned"] == 0
    assert ClaimRepository(connection).get_claim("claim")["expires_at"] is None


def test_open_preference_is_recognized_without_operational_slot() -> None:
    assert _is_preference_claim(
        {
            "predicate": "prefers",
            "canonical_attribute": "preference.architecture",
            "canonical_slot": None,
        }
    )


def test_recall_result_serializes_slot_tags_and_legacy_attribute(tmp_path) -> None:
    connection = Database(tmp_path / "recall-output.db").open()
    service = RecallService(connection, FakeEmbedder(8))
    result = service._assemble_results(
        [
            {
                "id": "claim",
                "value": "深色",
                "status": "active",
                "confidence": 0.9,
                "valid_from": "2026-01-01T00:00:00+00:00",
                "superseded_by_id": None,
                "canonical_attribute": "preference.ui_theme",
                "canonical_slot": "preference.ui_theme",
                "topic_tags": ["preference"],
            }
        ]
    )[0]
    assert result["canonical_attribute"] == "preference.ui_theme"
    assert result["canonical_slot"] == "preference.ui_theme"
    assert result["topic_tags"] == ["preference"]


@pytest.mark.parametrize("cron", ["3:00", "03:0", "24:00", "03:60", "03:00x"])
def test_settings_rejects_non_strict_dedup_cron(cron: str) -> None:
    with pytest.raises(ConfigurationError, match="HL_MEM_DEDUP_CRON"):
        replace(Settings(), dedup_cron=cron)._validate()


def test_dedup_apply_rechecks_confidence_and_claim_versions(tmp_path) -> None:
    connection = Database(tmp_path / "dedup-cas.db").open()
    repo = ClaimRepository(connection)
    base = {
        "namespace_key": "default",
        "predicate": "事实",
        "recorded_from": "2026-01-01T00:00:00+00:00",
        "status": "active",
        "canonical_slot": None,
    }
    repo.insert_claim({**base, "id": "left", "subject_entity_id": "left", "value": "same"})
    repo.insert_claim({**base, "id": "right", "subject_entity_id": "right", "value": "same"})
    connection.execute(
        "INSERT INTO dedup_pairs("
        "id,pair_key,left_claim_id,right_claim_id,similarity,decision,judge_confidence,created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        ("pair", "pair-key", "left", "right", 0.99, "equivalent", 0.97, base["recorded_from"]),
    )
    connection.commit()
    left = repo.get_claim("left")
    right = repo.get_claim("right")
    assert left is not None and right is not None
    assert not _apply_equivalent_pair(connection, "pair", left, right, base["recorded_from"], 0.98)
    connection.execute("UPDATE dedup_pairs SET judge_confidence=0.99 WHERE id='pair'")
    connection.execute("UPDATE claims SET recorded_from='2026-01-02T00:00:00+00:00' WHERE id='right'")
    connection.commit()
    assert not _apply_equivalent_pair(connection, "pair", left, right, base["recorded_from"], 0.98)
    assert repo.get_claim("right")["status"] == "active"


def test_cross_subject_candidate_limit_bounds_inputs_and_reuses_vectors(tmp_path) -> None:
    connection = Database(tmp_path / "bounded-candidates.db").open()
    repo = ClaimRepository(connection)
    embedder = FakeEmbedder(8)
    vector = embedder.embed_one("same")
    for index in range(3):
        repo.insert_claim(
            {
                "id": f"c{index}",
                "namespace_key": "default",
                "subject_entity_id": f"subject-{index}",
                "predicate": "事实",
                "value": "same",
                "recorded_from": f"2026-01-0{index + 1}T00:00:00+00:00",
                "status": "active",
                "canonical_slot": None,
                "embedding_dense": vector,
            }
        )

    class FailingEmbedder:
        """若候选发现错误调用远程向量化，立即让测试失败。"""

        def embed_batch(self, texts: list[str]) -> list[bytes]:
            raise AssertionError(f"unexpected embedding call for {len(texts)} texts")

    pairs = repo.find_cross_subject_dedup_candidates(
        "default",
        FailingEmbedder(),
        threshold=0.99,
        limit=2,
    )
    assert len(pairs) == 1
    assert {pairs[0]["left"]["id"], pairs[0]["right"]["id"]} == {"c1", "c2"}


def test_conflict_key_v3_backfill_preserves_previous_key(tmp_path) -> None:
    connection = Database(tmp_path / "conflict-v3.db").open()
    connection.execute("DELETE FROM schema_migrations WHERE version=?", (DATA_MIGRATION_VERSION,))
    ClaimRepository(connection).insert_claim(
        {
            "id": "claim",
            "namespace_key": "default",
            "subject_entity_id": "service",
            "predicate": "配置",
            "value": "8200",
            "qualifiers": {"service": "api"},
            "canonical_slot": "config.port",
            "conflict_key": "old-v2-key",
            "conflict_key_version": 2,
            "recorded_from": "2026-01-01T00:00:00+00:00",
            "status": "active",
        }
    )
    assert backfill_conflict_keys_v3(connection) == 1
    claim = ClaimRepository(connection).get_claim("claim")
    assert claim is not None
    assert claim["conflict_key_version"] == 3
    assert claim["legacy_conflict_key"] == "old-v2-key"
    assert claim["conflict_key"] != "old-v2-key"
    assert backfill_conflict_keys_v3(connection) == 0
