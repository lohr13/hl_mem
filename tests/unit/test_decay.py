"""记忆衰减与归档策略测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.database import Database
from hl_mem.storage.claims import ClaimRepository
from hl_mem.workers.decay import decay_claims
from hl_mem.workers.worker import Worker, dispatch_job

NOW = "2026-07-21T00:00:00+00:00"


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
    decay_claims(connection, NOW)
    assert connection.execute("SELECT status FROM claims").fetchone()[0] == expected


def test_decay_access_count_bonus_extends_threshold(tmp_path):
    """访问次数应延长 temporal 记忆的衰减阈值。"""
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=200)).isoformat()
    _claim(connection, scope="temporal", recorded_from=recorded, last_accessed_at=recorded, access_count=50)
    decay_claims(connection, NOW)
    assert connection.execute("SELECT status FROM claims").fetchone()[0] == "active"

    connection2 = _decay_db(tmp_path)
    recorded2 = (datetime.fromisoformat(NOW) - timedelta(days=400)).isoformat()
    _claim(connection2, "c2", scope="temporal", recorded_from=recorded2, last_accessed_at=recorded2, access_count=50)
    decay_claims(connection2, NOW)
    assert connection2.execute("SELECT status FROM claims WHERE id='c2'").fetchone()[0] == "archived"


def test_decay_access_count_bonus_capped_at_365(tmp_path):
    """访问奖励最多延长 365 天。"""
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=500)).isoformat()
    _claim(connection, scope="temporal", recorded_from=recorded, last_accessed_at=recorded, access_count=1000)
    decay_claims(connection, NOW)
    assert connection.execute("SELECT status FROM claims").fetchone()[0] == "active"


def test_decay_elapsed_linear_once_daily_and_floor(tmp_path):
    connection = _decay_db(tmp_path)
    recorded = (datetime.fromisoformat(NOW) - timedelta(days=100)).isoformat()
    _claim(connection, scope="temporal", recorded_from=recorded, last_accessed_at=recorded, confidence=0.08)
    assert decay_claims(connection, NOW) == {"decayed": 1, "archived": 0}
    assert connection.execute("SELECT confidence FROM claims").fetchone()[0] == pytest.approx(0.05)
    assert decay_claims(connection, "2026-07-21T12:00:00+00:00")["decayed"] == 0


def test_decay_archive_keeps_evidence_and_clears_embedding(tmp_path):
    connection = _decay_db(tmp_path)
    old = "2025-01-01T00:00:00+00:00"
    _claim(connection, recorded_from=old, last_accessed_at=old)
    connection.execute(
        "INSERT INTO evidence_links(id,derived_type,derived_id,evidence_type,evidence_id,relation) "
        "VALUES ('l','claim','c','event','e','derived_from')"
    )
    connection.commit()
    decay_claims(connection, NOW)
    row = connection.execute("SELECT status,embedding_dense FROM claims").fetchone()
    assert tuple(row) == ("archived", None)
    assert connection.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == 1


def test_decay_rollout_grace_exempts_preexisting_unaccessed(tmp_path):
    connection = _decay_db(tmp_path)
    connection.execute(
        "UPDATE schema_migrations SET applied_at='2026-07-20 00:00:00' WHERE version='005_memory_management'"
    )
    _claim(connection, recorded_from="2020-01-01T00:00:00+00:00", last_accessed_at=None)
    assert decay_claims(connection, NOW)["archived"] == 0


def test_worker_decay_dispatch(tmp_path):
    worker = Worker(tmp_path / "worker.db", {"embedding_dim": 2})
    assert dispatch_job(worker, {"job_type": "decay_access"}) == {"decayed": 0, "archived": 0}
    worker.database.close()
