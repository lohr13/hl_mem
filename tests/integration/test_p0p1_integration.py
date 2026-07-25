"""v0.12.0 P0/P1 修复的 SQLite 跨层回归矩阵。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.protocols import RelationProposal
from hl_mem.workers.discover_relations import discover_relations
from hl_mem.workers.ttl import expire_claims


def _configure_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定 API 工厂为无网络且启用 feedback 生命周期的测试配置。"""
    monkeypatch.setenv("HL_MEM_ENV", "test")
    monkeypatch.setenv("HL_MEM_EMBEDDER", "fake")
    monkeypatch.setenv("HL_MEM_RERANKER", "off")
    monkeypatch.setenv("HL_MEM_QUERY_EXPANSION_MODE", "off")
    monkeypatch.setenv("HL_MEM_RELATION_DISCOVERY_MODE", "off")
    monkeypatch.setenv("HL_MEM_FEEDBACK_LIFECYCLE_MODE", "on")


def test_procedure_episode_feedback_reaches_usefulness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API → procedure recall → episode exposure → feedback → usefulness。"""
    _configure_test_runtime(monkeypatch)
    app = create_app(tmp_path / "episode-feedback.db")
    with TestClient(app) as client:
        created = client.post("/v1/episodes", json={"goal": "deploy literal service"}).json()
        episode_id = created["id"]
        client.patch(
            f"/v1/episodes/{episode_id}",
            json={"status": "success", "reward": 1.0, "outcome_summary": "deployed"},
        )
        recalled = client.post(
            "/v1/recall",
            json={"query": "deploy literal service", "intent": "procedure", "limit": 5},
        ).json()
        episode = next(item for item in recalled["results"] if item["memory_type"] == "episode")
        response = client.post(
            "/v1/feedback",
            json={"feedback_id": episode["feedback_id"], "helpful": True, "task_outcome": 1.0},
        )
        assert response.status_code == 200
        row = app.state.db.open().execute(
            "SELECT helpful_count,outcome_count FROM memory_usefulness "
            "WHERE memory_type=? AND memory_id=?",
            ("episode", episode_id),
        ).fetchone()
        assert tuple(row) == (1, 1)


def test_api_feedback_bonus_flows_into_ttl_valid_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API → 三次反馈 → TTL worker → effective valid_to。"""
    _configure_test_runtime(monkeypatch)
    app = create_app(tmp_path / "feedback-ttl.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        connection.execute(
            "INSERT INTO claims(id,status,subject_entity_id,predicate,value_json,recorded_from,scope,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "ttl-claim",
                "active",
                "service",
                "state",
                '"degraded"',
                "2026-07-01T00:00:00+00:00",
                "temporal",
                "2026-07-20T00:00:00+00:00",
            ),
        )
        connection.commit()
        for _ in range(3):
            recalled = client.post(
                "/v1/recall",
                json={"query": "service degraded", "limit": 1, "as_of": "2026-07-21T00:00:00+00:00"},
            ).json()
            client.post(
                "/v1/feedback",
                json={"feedback_id": recalled["results"][0]["feedback_id"], "helpful": True},
            )

        assert expire_claims(connection, "2026-08-04T00:00:00+00:00", "on") == {"expired": 1}
        row = connection.execute("SELECT valid_to FROM claims WHERE id=?", ("ttl-claim",)).fetchone()
        assert row["valid_to"] == "2026-08-03T00:00:00+00:00"


class _ArchivingDiscoverer:
    """在模型阶段模拟 endpoint 被另一生命周期流程归档。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def propose(self, source_claim, candidates, *, max_proposals):
        del max_proposals
        self.connection.execute("UPDATE claims SET status='archived' WHERE id=?", ("target",))
        self.connection.commit()
        return [
            RelationProposal(
                str(source_claim["id"]),
                str(candidates[0]["id"]),
                "supports",
                0.99,
                "integration stale endpoint",
                (),
                "fake",
            )
        ]


def test_relation_discovery_rejects_endpoint_archived_during_proposal(tmp_path: Path) -> None:
    """discover → archive endpoint → auto apply → rejected。"""
    database_path = tmp_path / "relation-stale.db"
    app = create_app(database_path)
    connection = app.state.db.open()
    connection.executemany(
        "INSERT INTO claims(id,status,predicate,value_json,recorded_from) VALUES(?,?,?,?,?)",
        (
            ("source", "active", "p", '"source"', "2026-01-01T00:00:00+00:00"),
            ("target", "active", "p", '"target"', "2026-01-02T00:00:00+00:00"),
        ),
    )
    connection.commit()

    result = discover_relations(
        connection,
        _ArchivingDiscoverer(connection),
        "source",
        mode="auto",
        pool_limit=5,
        max_proposals=1,
        auto_apply_confidence=0.8,
        conflict_confidence=0.9,
    )
    proposal = connection.execute("SELECT status,decision_reason FROM relation_proposals").fetchone()
    assert result["applied"] == 0
    assert tuple(proposal) == ("rejected", "stale-input")
    app.state.db.close()


def test_percent_query_only_returns_literal_procedure_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API procedure recall 中的 % 不得扩大到全部 Episode。"""
    _configure_test_runtime(monkeypatch)
    app = create_app(tmp_path / "procedure-percent.db")
    with TestClient(app) as client:
        connection = app.state.db.open()
        connection.executemany(
            "INSERT INTO episodes(id,goal,status,started_at,ended_at,reward) VALUES(?,?,?,?,?,?)",
            (
                (
                    "literal",
                    "deploy % service",
                    "success",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    1.0,
                ),
                (
                    "unrelated",
                    "deploy any service",
                    "success",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    1.0,
                ),
            ),
        )
        connection.commit()

        recalled = client.post(
            "/v1/recall",
            json={"query": "%", "intent": "procedure", "limit": 10},
        ).json()
        episode_ids = [item["id"] for item in recalled["results"] if item["memory_type"] == "episode"]
        assert episode_ids == ["literal"]
