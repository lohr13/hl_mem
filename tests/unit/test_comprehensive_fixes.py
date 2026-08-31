"""综合事务、安全边界与运行环境回归测试。"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from hl_mem.adapters.hermes.renderer import render_context
from hl_mem.api import server
from hl_mem.components import make_embedder, make_reranker
from hl_mem.errors import ConfigurationError
from hl_mem.experience.service import ExperienceService, backprop_episode_reward
from hl_mem.ingest.extractors import FakeExtractor
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.jobs import JobRepository
from hl_mem.workers.worker import Worker


def test_database_open_returns_independent_connections(tmp_path) -> None:
    """普通 open 调用不得共享同一个 SQLite Connection。"""
    database = Database(tmp_path / "pool.db")
    first = database.open()
    second = database.open()
    try:
        assert first is not second
    finally:
        database.close()


def test_database_open_sets_busy_timeout(tmp_path) -> None:
    """数据库连接必须采用配置的锁等待超时。"""
    database = Database(tmp_path / "busy-timeout.db", busy_timeout_seconds=30)
    connection = database.open()
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
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

    threads = [threading.Thread(target=open_database, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert not [thread.name for thread in threads if thread.is_alive()], "concurrent migration threads did not finish"
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
    app = server.create_app(tmp_path / "event-rollback.db")
    monkeypatch.setattr(
        "hl_mem.application.ingest.IngestService._queue_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue")),
    )
    with pytest.raises(RuntimeError, match="queue"), TestClient(app) as client:
        client.post("/v1/events", json={"content": "测试"})
    connection = app.state.db.open()
    try:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        connection.close()


def test_real_embedder_and_reranker_require_keys() -> None:
    """真实外部模型组件缺少密钥时必须直接失败。"""
    settings = Settings(
        embedder_mode="real",
        reranker_mode="real",
        extractor_mode="real",
    )
    with pytest.raises(ConfigurationError, match="EMBEDDING_API_KEY"):
        make_embedder(settings)
    with pytest.raises(ConfigurationError, match="RERANKER_API_KEY|EMBEDDING_API_KEY"):
        make_reranker(settings)


def test_worker_real_extractor_fails_without_key() -> None:
    """真实 Worker 提取器缺少密钥时不得静默降级。"""
    worker = Worker.__new__(Worker)
    worker.settings = Settings(extractor_mode="real")
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        worker._make_extractor()


def test_worker_fake_extractor_is_safe_default() -> None:
    """静态默认配置允许 Worker 使用 FakeExtractor。"""
    worker = Worker.__new__(Worker)
    worker.settings = Settings(extractor_mode="fake")

    assert isinstance(worker._make_extractor(), FakeExtractor)


def test_health_reports_fake_components(tmp_path) -> None:
    """健康检查暴露当前模型组件是否为降级实现。"""
    settings = Settings(
        database_path=str(tmp_path / "health.db"),
        embedder_mode="fake",
        reranker_mode="fake",
    )
    with TestClient(server.create_app(settings)) as client:
        body = client.get("/healthz").json()
    assert body["embedder"] == "fake"
    assert body["reranker"] == "fake"


def test_health_reports_open_conflict_count(tmp_path) -> None:
    """健康检查暴露所有未决状态且尚未解决的冲突数量。"""
    app = server.create_app(tmp_path / "health-conflicts.db")
    with TestClient(app) as client:
        before = client.get("/healthz").json()
        with app.state.db.connect() as connection:
            repository = ClaimRepository(connection)
            for claim_id, value in (("left", "SQLite"), ("right", "PostgreSQL")):
                assert repository.insert_claim(
                    {
                        "id": claim_id,
                        "namespace_key": "default",
                        "subject_entity_id": "用户",
                        "predicate": "使用",
                        "value": value,
                        "status": "disputed",
                        "recorded_from": "2026-01-01T00:00:00+00:00",
                    }
                )
            assert repository.insert_conflict_case(
                {
                    "id": "case",
                    "pair_key": "left:right",
                    "left_claim_id": "left",
                    "right_claim_id": "right",
                    "status": "manual_required",
                    "created_at": "2026-01-02T00:00:00+00:00",
                }
            )
        after = client.get("/healthz").json()

    assert after["conflict_open_count"] == before["conflict_open_count"] + 1
    assert after["manual_required_count"] == before["manual_required_count"] + 1
    assert after["conflict_counts_by_status"]["manual_required"] >= 1
    assert after["oldest_manual_required_age_seconds"] > 0


def test_health_reports_dangling_conflict_categories(tmp_path) -> None:
    app = server.create_app(tmp_path / "health-dangling-conflicts.db")
    with TestClient(app) as client:
        with app.state.db.connect() as connection:
            repository = ClaimRepository(connection)
            assert repository.insert_claim(
                {
                    "id": "existing-left",
                    "namespace_key": "default",
                    "subject_entity_id": "gateway",
                    "predicate": "uses",
                    "value": "SQLite",
                    "status": "disputed",
                    "recorded_from": "2026-08-16T00:00:00+00:00",
                }
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
            for values in (
                ("terminal-both", "missing-a", "missing-b", "resolved"),
                ("terminal-one", "existing-left", "missing-c", "rejected"),
                ("open-both", "missing-d", "missing-e", "manual_required"),
            ):
                case_id, left_id, right_id, status = values
                connection.execute(
                    "INSERT INTO conflict_cases("
                    "id,pair_key,left_claim_id,right_claim_id,status,created_at,resolved_at"
                    ") VALUES (?,?,?,?,?,?,?)",
                    (
                        case_id,
                        f"pair:{case_id}",
                        left_id,
                        right_id,
                        status,
                        "2026-08-16T00:00:00+00:00",
                        "2026-08-16T00:00:00+00:00" if status in {"resolved", "rejected"} else None,
                    ),
                )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")

        body = client.get("/healthz").json()

    assert body["conflict_dangling"] == {
        "terminal_both_missing": 1,
        "terminal_one_side": 1,
        "open_dangling": 1,
    }


def test_recall_feedback_failure_does_not_change_main_result(tmp_path, monkeypatch) -> None:
    """召回曝光投递失败时仍返回主召回结果。"""
    app = server.create_app(tmp_path / "recall-feedback.db")
    monkeypatch.setattr(app.state.recall_side_effects, "submit_exposures", lambda *args, **kwargs: False)
    connection = app.state.db.open()
    ClaimRepository(connection).insert_claim(
        {
            "id": "claim-1",
            "status": "active",
            "subject_entity_id": "user",
            "predicate": "likes",
            "value": "tea",
            "index_text": "user likes tea",
            "recorded_from": "2026-07-22T00:00:00+00:00",
        }
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/recall",
            json={
                "query": "likes tea",
                "limit": 1,
                "response_format": "context_packet",
            },
        )
        assert connection.execute("SELECT count(*) FROM retrieval_feedback").fetchone()[0] == 0
    assert response.status_code == 200
    packet = response.json()["context_packet"]
    assert packet["feedback_state"] == "degraded"
    assert packet["items"][0]["id"] == "claim-1"
    assert packet["items"][0]["feedback_id"]


def test_context_packet_projects_stored_claim_relation_for_reader(tmp_path) -> None:
    app = server.create_app(tmp_path / "recall-relation.db")
    connection = app.state.db.open()
    ClaimRepository(connection).insert_claim(
        {
            "id": "claim-1",
            "status": "active",
            "subject_entity_id": "团队",
            "predicate": "采用",
            "value": "海风看板",
            "qualifiers": {},
            "index_text": "团队后来采用海风看板",
            "recorded_from": "2026-07-22T00:00:00+00:00",
        }
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/recall",
            json={"query": "团队采用海风看板", "limit": 1, "response_format": "context_packet"},
        )

    assert response.status_code == 200
    packet = response.json()["context_packet"]
    assert packet["items"][0].get("role") == "团队"
    assert packet["items"][0].get("action") == "采用"
    assert packet["items"][0].get("object") == "海风看板"
    assert render_context(packet).text == "团队后来采用海风看板\nrelation: 团队 → 采用 → 海风看板"


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
        "UPDATE policies SET status='retired',procedure_status='retired' WHERE id=?",
        (policy_id,),
    )
    service.connection.commit()
    with pytest.raises(ValueError, match="retired"):
        service.add_support(policy_id, "e1")
    with pytest.raises(ValueError, match="retired"):
        service.record_policy_outcome(policy_id, True, "2026-01-02T00:00:00Z")


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
