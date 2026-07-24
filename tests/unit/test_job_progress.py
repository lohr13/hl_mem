"""后台任务进度持久化测试。"""

from __future__ import annotations

from hl_mem.storage.database import Database
from hl_mem.storage.jobs import JobRepository


def _lease_job(tmp_path):
    connection = Database(tmp_path / "jobs.db").open()
    repository = JobRepository(connection)
    repository.insert_job(
        {
            "id": "job-1",
            "job_type": "deduplicate_claims",
            "payload_json": "{}",
            "created_at": "2026-07-24T00:00:00+00:00",
            "updated_at": "2026-07-24T00:00:00+00:00",
        }
    )
    job = repository.lease_job("2026-07-24T01:00:00+00:00", "2026-07-24T00:00:01+00:00")
    assert job is not None
    return connection, repository, job


def test_update_progress_with_lease_token(tmp_path) -> None:
    """持有当前 lease token 的 Worker 可以更新任务进度。"""
    connection, repository, job = _lease_job(tmp_path)

    updated = repository.update_progress(
        job["id"],
        job["lease_token"],
        stage="review",
        processed=3,
        total=10,
        detail={"candidate": "pair-3"},
        heartbeat_at="2026-07-24T00:00:02+00:00",
    )

    row = connection.execute("SELECT * FROM jobs WHERE id='job-1'").fetchone()
    assert updated is True
    assert (row["stage"], row["processed"], row["total"], row["heartbeat_at"]) == (
        "review",
        3,
        10,
        "2026-07-24T00:00:02+00:00",
    )
    assert row["progress_detail_json"] == '{"candidate": "pair-3"}'


def test_update_progress_rejected_without_lease(tmp_path) -> None:
    """错误 lease token 不得修改运行中任务。"""
    connection, repository, _job = _lease_job(tmp_path)

    assert repository.update_progress("job-1", "wrong-token", processed=7) is False
    assert connection.execute("SELECT processed FROM jobs WHERE id='job-1'").fetchone()[0] == 0


def test_progress_fields_in_job_dict(tmp_path) -> None:
    """任务字典应包含进度字段并解码 detail JSON。"""
    _connection, repository, job = _lease_job(tmp_path)
    repository.update_progress(
        job["id"],
        job["lease_token"],
        stage="scan",
        processed=1,
        total=4,
        detail={"namespace": "default"},
        heartbeat_at="2026-07-24T00:00:03+00:00",
    )

    listed = repository.list_jobs()

    assert listed[0]["stage"] == "scan"
    assert listed[0]["processed"] == 1
    assert listed[0]["total"] == 4
    assert listed[0]["heartbeat_at"] == "2026-07-24T00:00:03+00:00"
    assert listed[0]["progress_detail"] == {"namespace": "default"}


def test_job_progress_migration_is_registered(tmp_path) -> None:
    """打开数据库应应用并注册 020 migration。"""
    connection = Database(tmp_path / "migration.db").open()
    version = connection.execute(
        "SELECT version FROM schema_migrations WHERE version='020_job_progress'"
    ).fetchone()
    assert version is not None
