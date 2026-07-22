"""综合事务、安全边界与运行环境回归测试。"""

from __future__ import annotations

import sqlite3
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from hl_mem.api import server
from hl_mem.experience.service import ExperienceService, backprop_episode_reward
from hl_mem.ingest.budget import TokenBudget
from hl_mem.ingest.embeddings import Embedder
from hl_mem.storage.database import Database
from hl_mem.storage.repository import EventRepository, JobRepository


def test_database_open_returns_independent_connections(tmp_path) -> None:
    """普通 open 调用不得共享同一个 SQLite Connection。"""
    database = Database(tmp_path / "pool.db")
    first = database.open()
    second = database.open()
    try:
        assert first is not second
    finally:
        database.close()


def test_concurrent_database_instances_apply_migrations_once(tmp_path) -> None:
    """不同 Database 实例并发启动时迁移版本检查与执行必须原子化。"""
    path = tmp_path / "concurrent-migration.db"
    errors: list[Exception] = []

    def open_database() -> None:
        database = Database(path)
        try:
            database.open().close()
        except Exception as error:
            errors.append(error)
        finally:
            database.close()

    threads = [threading.Thread(target=open_database) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with sqlite3.connect(path) as connection:
        versions = connection.execute("SELECT version,count(*) FROM schema_migrations GROUP BY version").fetchall()
    assert versions
    assert all(count == 1 for _, count in versions)


def test_repository_commit_false_allows_atomic_event_and_job_rollback(tmp_path) -> None:
    """事件和任务可由上层放进同一事务并整体回滚。"""
    connection = Database(tmp_path / "atomic.db").open()
    connection.execute("BEGIN")
    EventRepository(connection).insert_event(
        {
            "id": "event-1",
            "tenant_id": "default",
            "event_type": "message",
            "actor_type": "user",
            "content_json": "{}",
            "occurred_at": "2026-01-01T00:00:00Z",
            "recorded_at": "2026-01-01T00:00:00Z",
        },
        commit=False,
    )
    JobRepository(connection).insert_job(
        {
            "id": "job-1",
            "job_type": "extract_event",
            "payload_json": "{}",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        commit=False,
    )
    connection.rollback()
    assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_event_api_rolls_back_event_when_job_enqueue_fails(tmp_path, monkeypatch) -> None:
    """任务入队异常时 API 不得留下孤立 Event。"""
    monkeypatch.setenv("HL_MEM_ENV", "test")
    app = server.create_app(tmp_path / "event-rollback.db")
    monkeypatch.setattr(server, "_queue_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue")))
    with pytest.raises(RuntimeError, match="queue"), TestClient(app) as client:
        client.post("/v1/events", json={"content": "测试"})
    connection = app.state.db.open()
    try:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        connection.close()


def test_production_requires_real_embedder_and_reranker(monkeypatch) -> None:
    """生产环境缺少外部模型密钥时必须启动失败。"""
    monkeypatch.setenv("HL_MEM_ENV", "production")
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("RERANKER_API_KEY", raising=False)
    monkeypatch.delenv("HL_MEM_EMBEDDER", raising=False)
    monkeypatch.delenv("HL_MEM_RERANKER", raising=False)
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        server._make_embedder()
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="RERANKER_API_KEY|EMBEDDING_API_KEY"):
        server._make_reranker()


def test_health_reports_fake_components_in_test_environment(tmp_path, monkeypatch) -> None:
    """健康检查暴露当前模型组件是否为降级实现。"""
    monkeypatch.setenv("HL_MEM_ENV", "test")
    monkeypatch.setenv("HL_MEM_EMBEDDER", "fake")
    monkeypatch.setenv("HL_MEM_RERANKER", "fake")
    with TestClient(server.create_app(tmp_path / "health.db")) as client:
        body = client.get("/healthz").json()
    assert body["embedder"] == "fake"
    assert body["reranker"] == "fake"


def test_recall_feedback_failure_does_not_change_main_result(tmp_path, monkeypatch) -> None:
    """召回曝光批量写入失败时仍返回主召回结果。"""
    monkeypatch.setenv("HL_MEM_ENV", "test")
    monkeypatch.setattr(
        ExperienceService,
        "record_feedback_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("feedback")),
    )
    with TestClient(server.create_app(tmp_path / "recall-feedback.db")) as client:
        response = client.post("/v1/recall", json={"query": "不存在"})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_episode_state_machine_reward_and_terminal_trace_guards(tmp_path) -> None:
    """Episode 只允许 running 进入终态，reward 限定在 [0, 1]。"""
    service = ExperienceService(Database(tmp_path / "episode.db").open())
    service.create_episode("e1", "修复", "2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="reward"):
        service.update_episode("e1", "2026-01-01T01:00:00Z", reward=1.1)
    service.update_episode("e1", "2026-01-01T01:00:00Z", status="success", reward=1.0)
    with pytest.raises(ValueError, match="transition"):
        service.update_episode("e1", "2026-01-01T02:00:00Z", status="failed")
    with pytest.raises(ValueError, match="terminal"):
        service.add_trace("e1", "late", None, None, 0.0)
    with pytest.raises(ValueError, match="reward"):
        backprop_episode_reward(service.connection, "e1", -0.1)


def test_episode_api_returns_conflict_for_illegal_transition(tmp_path, monkeypatch) -> None:
    """非法状态转换由 API 映射为 HTTP 409。"""
    monkeypatch.setenv("HL_MEM_ENV", "test")
    with TestClient(server.create_app(tmp_path / "episode-api.db")) as client:
        episode_id = client.post("/v1/episodes", json={"goal": "修复"}).json()["id"]
        assert client.patch(f"/v1/episodes/{episode_id}", json={"status": "success"}).status_code == 200
        assert client.patch(f"/v1/episodes/{episode_id}", json={"status": "failed"}).status_code == 409
        assert client.post(f"/v1/episodes/{episode_id}/traces", json={"action": "late"}).status_code == 409


def test_policy_operations_reject_missing_and_retired_policy(tmp_path) -> None:
    """策略证据和结果不能写入不存在或已退休的策略。"""
    service = ExperienceService(Database(tmp_path / "policy.db").open())
    service.record_episode("e1", "修复", "success", 1.0, "2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="policy not found"):
        service.add_support("missing", "e1")
    with pytest.raises(ValueError, match="policy not found"):
        service.record_policy_outcome("missing", True, "2026-01-01T00:00:00Z")
    policy_id = service.induce_policy("修复", {"steps": ["测试"]}, ["e1"], "2026-01-01T00:00:00Z")
    service.connection.execute(
        "UPDATE policies SET status='retired',procedure_status='retired' WHERE id=?", (policy_id,)
    )
    service.connection.commit()
    with pytest.raises(ValueError, match="retired"):
        service.add_support(policy_id, "e1")
    with pytest.raises(ValueError, match="retired"):
        service.record_policy_outcome(policy_id, True, "2026-01-02T00:00:00Z")


def test_embedding_retries_retryable_status_with_configured_timeout(monkeypatch) -> None:
    """Embedding 对 429 重试，并使用拆分的连接/读取超时。"""
    attempts: list[httpx.Timeout] = []

    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "retry", request=httpx.Request("POST", "https://example.test"), response=self
                )

        def json(self) -> dict[str, object]:
            return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}

    responses = iter([Response(429), Response(200)])

    def post(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        return next(responses)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("hl_mem.ingest.embeddings.time.sleep", lambda _: None)
    assert len(Embedder("key", "https://example.test", "model", 2).embed_one("文本")) == 8
    assert len(attempts) == 2
    assert attempts[0].connect == 5.0
    assert attempts[0].read == 30.0


def test_budget_uses_sqlite_atomic_updates(tmp_path) -> None:
    """多个预算实例必须通过 SQLite 原子累加而不丢失更新。"""
    path = tmp_path / "budget.db"
    first, second = TokenBudget(10, path), TokenBudget(10, path)
    first.record_usage(3)
    second.record_usage(4)
    assert first.get_stats()["used_tokens"] == 7
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT used_tokens FROM token_budget").fetchone()[0] == 7


def test_experience_schema_has_status_checks(tmp_path) -> None:
    """新数据库 schema 在存储层拒绝非法 Episode/Policy 状态。"""
    connection = Database(tmp_path / "checks.db").open()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO episodes(id,goal,status,started_at) VALUES ('bad','x','unknown','2026-01-01')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO policies(id,trigger,procedure,status,created_at,updated_at) "
            "VALUES ('bad','x','{}','unknown','2026-01-01','2026-01-01')"
        )
