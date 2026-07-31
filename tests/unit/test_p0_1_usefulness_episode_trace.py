"""P0-1：episode/trace usefulness 数据库约束回归测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hl_mem.storage.database import Database
from hl_mem.storage.usefulness import UsefulnessRepository


def _seed_episode_and_trace(connection: sqlite3.Connection) -> None:
    """写入可供 usefulness 外键语义校验使用的 Episode 与 Trace。"""
    connection.execute(
        "INSERT INTO episodes(id,goal,status,started_at) VALUES(?,?,?,?)",
        ("episode-1", "完成任务", "success", "2026-01-01T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO traces(id,episode_id,sequence_no,action) VALUES(?,?,?,?)",
        ("trace-1", "episode-1", 1, "执行步骤"),
    )


def test_episode_and_trace_feedback_can_be_aggregated(tmp_path: Path) -> None:
    """约束遗漏 episode/trace 时，真实聚合写入必须失败。"""
    database = Database(tmp_path / "episode-trace.db")
    connection = database.open()
    try:
        _seed_episode_and_trace(connection)
        repository = UsefulnessRepository(connection)

        episode = repository.upsert("episode", "episode-1", helpful_delta=1)
        trace = repository.upsert("trace", "trace-1", helpful_delta=1)

        assert episode.memory_type == "episode"
        assert trace.memory_type == "trace"
    finally:
        database.close()


def test_rebuild_all_includes_episode_and_trace_feedback(tmp_path: Path) -> None:
    """全量重建不得遗漏已进入反馈事实表的 Episode 与 Trace。"""
    database = Database(tmp_path / "episode-trace-rebuild.db")
    connection = database.open()
    try:
        _seed_episode_and_trace(connection)
        connection.executemany(
            "INSERT INTO retrieval_feedback("
            "id,query_id,memory_type,memory_id,injected,helpful,created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                (
                    "feedback-episode",
                    "query-1",
                    "episode",
                    "episode-1",
                    1,
                    1,
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "feedback-trace",
                    "query-1",
                    "trace",
                    "trace-1",
                    1,
                    1,
                    "2026-01-01T00:00:00+00:00",
                ),
            ),
        )
        repository = UsefulnessRepository(connection)

        assert repository.rebuild_all() == 2
        assert repository.get("episode", "episode-1") is not None
        assert repository.get("trace", "trace-1") is not None
    finally:
        database.close()
