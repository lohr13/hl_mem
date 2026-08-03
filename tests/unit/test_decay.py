"""记忆衰减与归档策略测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from hl_mem.domain.claims.retention import TTLPolicy
from hl_mem.ingest.embedder import pack_vector
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.workers.backfill_expires_at import backfill_expires_at
from hl_mem.workers.decay import decay_claims
from hl_mem.workers.worker import Worker, dispatch_job

NOW = "2026-07-21T00:00:00+00:00"
DECAY_ARGS = {
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
}


def _claim(connection, claim_id="c", **values):
    data = {
        "id": claim_id,
        "recorded_from": NOW,
        "status": "active",
        "subject_entity_id": "user",
        "predicate": "likes",
        "value_json": '"tea"',
        "confidence": 1.0,
        "importance": 0.5,
        "embedding_dense": pack_vector([1.0]),
    }
    data.update(values)
    assert ClaimRepository(connection).insert_claim(data)
    return claim_id


def _decay_db(tmp_path):
    return Database(tmp_path / "decay.db").open()


class _BeforeBackfillUpdateConnection:
    """在 backfill CAS 前注入另一个连接的重分类提交。"""

    def __init__(self, connection: Any, before_update: Any) -> None:
        self._connection = connection
        self._before_update = before_update
        self._injected = False

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        if sql.startswith("UPDATE claims SET expires_at=") and not self._injected:
            self._injected = True
            self._before_update()
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


@pytest.mark.parametrize(
    ("scope", "days", "expected"),
    [
        ("temporal", 90, "active"),
        ("temporal", 181, "archived"),
        ("permanent", 180, "active"),
        ("permanent", 366, "archived"),
    ],
)
def test_decay_boundaries(tmp_path, scope, days, expected):
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=days)).isoformat()
    _claim(connection, scope=scope, recorded_from=recorded, last_accessed_at=recorded)
    decay_claims(connection, NOW, **DECAY_ARGS)
    assert connection.execute("SELECT status FROM claims").fetchone()[0] == expected


def test_decay_access_count_bonus_extends_threshold(tmp_path):
    """访问次数应延长 temporal 记忆的衰减阈值。"""
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=200)).isoformat()
    _claim(
        connection,
        scope="temporal",
        recorded_from=recorded,
        last_accessed_at=recorded,
        access_count=50,
    )
    decay_claims(connection, NOW, **DECAY_ARGS)
    assert connection.execute("SELECT status FROM claims").fetchone()[0] == "active"

    connection2 = _decay_db(tmp_path)
    recorded2 = (datetime.fromisoformat(NOW) - timedelta(days=400)).isoformat()
    _claim(
        connection2,
        "c2",
        scope="temporal",
        recorded_from=recorded2,
        last_accessed_at=recorded2,
        access_count=50,
    )
    decay_claims(connection2, NOW, **DECAY_ARGS)
    assert connection2.execute("SELECT status FROM claims WHERE id='c2'").fetchone()[0] == "archived"


def test_decay_access_count_bonus_capped_at_365(tmp_path):
    """访问奖励最多延长 365 天。"""
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=500)).isoformat()
    _claim(
        connection,
        scope="temporal",
        recorded_from=recorded,
        last_accessed_at=recorded,
        access_count=1000,
    )
    decay_claims(connection, NOW, **DECAY_ARGS)
    assert connection.execute("SELECT status FROM claims").fetchone()[0] == "active"


def test_decay_elapsed_linear_once_daily_and_floor(tmp_path):
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=100)).isoformat()
    _claim(
        connection,
        scope="temporal",
        recorded_from=recorded,
        last_accessed_at=recorded,
        confidence=0.08,
    )
    assert decay_claims(connection, NOW, **DECAY_ARGS) == {"decayed": 1, "archived": 0}
    assert connection.execute("SELECT confidence FROM claims").fetchone()[0] == pytest.approx(0.05)
    assert decay_claims(connection, "2026-07-21T12:00:00+00:00", **DECAY_ARGS)["decayed"] == 0


def test_decay_records_day_start_so_next_run_applies_one_full_day(tmp_path):
    connection = _decay_db(tmp_path)
    recorded = "2026-04-11T00:00:00+00:00"
    _claim(
        connection,
        scope="temporal",
        recorded_from=recorded,
        last_accessed_at=recorded,
    )

    assert decay_claims(connection, "2026-07-21T12:00:00+00:00", **DECAY_ARGS)["decayed"] == 1
    first = connection.execute("SELECT confidence,last_decayed_at FROM claims").fetchone()
    assert first[1] == "2026-07-21T00:00:00+00:00"

    assert decay_claims(connection, "2026-07-22T12:00:00+00:00", **DECAY_ARGS)["decayed"] == 1
    second = connection.execute("SELECT confidence,last_decayed_at FROM claims").fetchone()
    assert second[0] < first[0]
    assert second[1] == "2026-07-22T00:00:00+00:00"


def test_decay_normalizes_legacy_runtime_timestamp_before_increment(tmp_path):
    connection = _decay_db(tmp_path)
    recorded = "2026-04-11T00:00:00+00:00"
    _claim(
        connection,
        scope="temporal",
        recorded_from=recorded,
        last_accessed_at=recorded,
        last_decayed_at="2026-07-21T12:00:00+00:00",
        confidence=0.8,
    )

    assert decay_claims(connection, "2026-07-22T12:00:00+00:00", **DECAY_ARGS)["decayed"] == 1
    confidence, last_decayed_at = connection.execute("SELECT confidence,last_decayed_at FROM claims").fetchone()
    assert confidence < 0.8
    assert last_decayed_at == "2026-07-22T00:00:00+00:00"


@pytest.mark.parametrize("canonical_attribute", ["identity.name", "identity.role", "memory.explicit"])
def test_decay_exempts_active_core_attributes(tmp_path, canonical_attribute):
    connection = _decay_db(tmp_path)
    old = "2025-01-01T00:00:00+00:00"
    embedding = pack_vector([1.0])
    _claim(
        connection,
        recorded_from=old,
        last_accessed_at=old,
        canonical_attribute=canonical_attribute,
        confidence=0.8,
        embedding_dense=embedding,
    )

    assert decay_claims(connection, NOW, **DECAY_ARGS) == {"decayed": 0, "archived": 0}
    row = connection.execute("SELECT status,confidence,last_decayed_at,embedding_dense FROM claims").fetchone()
    assert tuple(row) == ("active", 0.8, None, embedding)


def test_expires_backfill_exempts_identity_attribute_family(tmp_path):
    connection = _decay_db(tmp_path)
    _claim(
        connection,
        recorded_from="2026-01-01T00:00:00+00:00",
        observed_at="2026-01-01T00:00:00+00:00",
        scope="temporal",
        canonical_attribute="identity.role",
    )

    result = backfill_expires_at(
        connection,
        TTLPolicy(),
        dry_run=False,
        now=datetime.fromisoformat(NOW),
    )

    assert result["skipped_protected"] == 1
    assert tuple(connection.execute("SELECT expires_at,status FROM claims").fetchone()) == (None, "active")


def test_expires_backfill_cas_rejects_concurrent_core_reclassification(tmp_path):
    database = Database(tmp_path / "backfill-reclassification.db")
    backfill_connection = database.open()
    concurrent_connection = database.open()
    try:
        _claim(
            backfill_connection,
            recorded_from="2026-01-01T00:00:00+00:00",
            observed_at="2026-01-01T00:00:00+00:00",
            scope="temporal",
            canonical_attribute="fact.capability",
        )

        def protect_claim() -> None:
            concurrent_connection.execute("UPDATE claims SET canonical_attribute='identity.role' WHERE id='c'")
            concurrent_connection.commit()

        result = backfill_expires_at(
            _BeforeBackfillUpdateConnection(backfill_connection, protect_claim),
            TTLPolicy(),
            dry_run=False,
            now=datetime.fromisoformat(NOW),
        )

        assert result["applied"] == 0
        assert result["cas_skipped"] == 1
        row = backfill_connection.execute(
            "SELECT canonical_attribute,expires_at,status FROM claims WHERE id='c'"
        ).fetchone()
        assert tuple(row) == ("identity.role", None, "active")
    finally:
        database.close()


def test_decay_archive_keeps_evidence_and_clears_embedding(tmp_path):
    connection = _decay_db(tmp_path)
    old = "2025-01-01T00:00:00+00:00"
    _claim(connection, recorded_from=old, last_accessed_at=old)
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('l','claim','c','event','e','derived_from')"
    )
    connection.commit()
    decay_claims(connection, NOW, **DECAY_ARGS)
    row = connection.execute("SELECT status,embedding_dense FROM claims").fetchone()
    assert tuple(row) == ("archived", None)
    assert connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == 1


def test_decay_rollout_grace_exempts_preexisting_unaccessed(tmp_path):
    connection = _decay_db(tmp_path)
    connection.execute(
        "UPDATE schema_migrations SET applied_at='2026-07-20 00:00:00' WHERE version='005_memory_management'"
    )
    _claim(connection, recorded_from="2020-01-01T00:00:00+00:00", last_accessed_at=None)
    assert decay_claims(connection, NOW, **DECAY_ARGS)["archived"] == 0


def test_worker_decay_dispatch(tmp_path):
    worker = Worker(Settings(database_path=str(tmp_path / "worker.db"), embedding_dim=2))
    assert dispatch_job(worker, {"job_type": "decay_access"}) == {
        "decayed": 0,
        "archived": 0,
    }
    worker.database.close()
