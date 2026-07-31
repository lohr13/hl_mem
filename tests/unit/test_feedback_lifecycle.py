"""反馈驱动生命周期的核心契约测试。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from hl_mem.domain.feedback import BayesianUsefulnessPolicy
from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.usefulness import UsefulnessRepository


def test_bayesian_prior_and_negative_feedback_do_not_create_bonus() -> None:
    """无反馈保持 0.5 prior，负反馈仅降低 usefulness。"""
    policy = BayesianUsefulnessPolicy()
    assert policy.evaluate(helpful_count=0, unhelpful_count=0, success_sum=0.0, outcome_count=0) == (0.5, 0)
    score, bonus = policy.evaluate(helpful_count=0, unhelpful_count=8, success_sum=0.0, outcome_count=0)
    assert score < 0.5
    assert bonus == 0


def test_positive_bonus_is_stepped_and_capped() -> None:
    """每三个正证据增加 14 天且不超过 cap。"""
    policy = BayesianUsefulnessPolicy(max_bonus_days=30)
    assert policy.evaluate(helpful_count=2, unhelpful_count=0, success_sum=0.0, outcome_count=0)[1] == 0
    assert policy.evaluate(helpful_count=3, unhelpful_count=0, success_sum=0.0, outcome_count=0)[1] == 14
    assert policy.evaluate(helpful_count=99, unhelpful_count=0, success_sum=0.0, outcome_count=0)[1] == 30


def test_settings_default_to_observe_and_validate_mode() -> None:
    """默认 rollout 只观察，非法模式拒绝启动。"""
    assert Settings().feedback_lifecycle_mode == "observe"
    with pytest.raises(Exception):
        replace(Settings(), feedback_lifecycle_mode="invalid").validate()


def test_usefulness_rebuild_matches_incremental_aggregate(tmp_path) -> None:
    """全量 rebuild 与增量提交得到相同聚合。"""
    database = Database(tmp_path / "feedback.db")
    connection = database.open()
    try:
        connection.execute(
            "INSERT INTO claims(id,recorded_from,status,scope) VALUES('c1','2026-01-01T00:00:00+00:00','active','temporal')"
        )
        connection.execute(
            "INSERT INTO retrieval_feedback(id,query_id,memory_type,memory_id,injected,helpful,task_outcome,created_at) "
            "VALUES('f1','q1','claim','c1',0,1,0.8,'2026-01-01T00:00:00+00:00')"
        )
        repository = UsefulnessRepository(connection)
        incremental = repository.upsert("claim", "c1", helpful_delta=1, success_delta=0.8, outcome_delta=1)
        repository.rebuild_all()
        rebuilt = repository.get("claim", "c1")
        assert rebuilt is not None
        assert rebuilt.helpful_count == incremental.helpful_count
        assert rebuilt.usefulness_score == pytest.approx(incremental.usefulness_score)
    finally:
        database.close()


def test_exposure_batch_materializes_only_delivery_neutral_receipts(tmp_path) -> None:
    """Exposure 物化固定为未注入且无隐式 helpful/outcome。"""
    database = Database(tmp_path / "exposure.db")
    connection = database.open()
    try:
        service = ExperienceService(connection)
        exposures = [
            ("f1", "q1", "claim", "c1", 1, 0.9, "2026-01-01T00:00:00+00:00"),
            ("f2", "q1", "policy", "p1", 2, 0.8, "2026-01-01T00:00:00+00:00"),
        ]

        assert service.record_exposure_batch(exposures) == 2
        rows = connection.execute(
            "SELECT id,rank,score,injected,helpful,task_outcome " "FROM retrieval_feedback ORDER BY rank"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("f1", 1, 0.9, 0, None, None),
            ("f2", 2, 0.8, 0, None, None),
        ]
    finally:
        database.close()


def test_legacy_feedback_batch_replay_remains_idempotent(tmp_path) -> None:
    """兼容十列 batch 重放仍忽略已存在的 receipt。"""
    database = Database(tmp_path / "legacy-feedback-batch.db")
    connection = database.open()
    try:
        service = ExperienceService(connection)
        feedback = [
            (
                "f1",
                "q1",
                "claim",
                "c1",
                1,
                0.9,
                0,
                None,
                None,
                "2026-01-01T00:00:00+00:00",
            )
        ]

        assert service.record_feedback_batch(feedback) == 1
        assert service.record_feedback_batch(feedback) == 0
        assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 1
    finally:
        database.close()


def test_exposure_batch_uses_savepoint_and_rolls_back_all_rows(tmp_path) -> None:
    """外层事务存在时，重复 receipt 只回滚 exposure batch，不破坏外层写入。"""
    database = Database(tmp_path / "exposure-rollback.db")
    connection = database.open()
    try:
        service = ExperienceService(connection)
        connection.execute("CREATE TEMP TABLE batch_sentinel(value TEXT NOT NULL)")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO batch_sentinel(value) VALUES('preserved')")
        duplicate = ("duplicate", "q1", "claim", "c1", 1, 0.9, "2026-01-01T00:00:00+00:00")

        with pytest.raises(sqlite3.IntegrityError):
            service.record_exposure_batch([duplicate, duplicate])

        assert connection.in_transaction
        assert connection.execute("SELECT value FROM batch_sentinel").fetchone()[0] == "preserved"
        assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 0
        connection.rollback()
    finally:
        database.close()


def test_mark_feedback_injected_batch_is_atomic_idempotent_and_strict(tmp_path) -> None:
    """Delivery 标记可重放；未知任一 ID 明确失败且不部分更新。"""
    database = Database(tmp_path / "injected.db")
    connection = database.open()
    try:
        service = ExperienceService(connection)
        service.record_exposure_batch(
            [
                ("f1", "q1", "claim", "c1", 1, 0.9, "2026-01-01T00:00:00+00:00"),
                ("f2", "q1", "claim", "c2", 2, 0.8, "2026-01-01T00:00:00+00:00"),
            ]
        )

        assert service.mark_feedback_injected_batch(["f1", "f1"]) == 1
        assert service.mark_feedback_injected_batch(["f1"]) == 0
        with pytest.raises(ValueError, match="feedback exposure not found: missing"):
            service.mark_feedback_injected_batch(["f2", "missing"])

        rows = connection.execute(
            "SELECT id,injected,helpful,task_outcome FROM retrieval_feedback ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("f1", 1, None, None),
            ("f2", 0, None, None),
        ]
    finally:
        database.close()


def test_invalid_memory_type_and_id_are_rejected(tmp_path) -> None:
    """聚合不能为不存在或非法类型的记忆创建孤儿行。"""
    database = Database(tmp_path / "invalid.db")
    connection = database.open()
    try:
        repository = UsefulnessRepository(connection)
        with pytest.raises(ValueError):
            repository.upsert("claim", "missing", helpful_delta=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            repository.upsert("episode", "missing", helpful_delta=1)  # type: ignore[arg-type]
    finally:
        database.close()
