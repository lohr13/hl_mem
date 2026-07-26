"""P1-5：Procedure 查询 LIKE 通配符转义测试。"""

from __future__ import annotations

from pathlib import Path

from hl_mem.storage._shared import escape_like_pattern
from hl_mem.storage.database import Database
from hl_mem.storage.experience import ExperienceRepository


def test_escape_like_pattern_treats_metacharacters_as_literals() -> None:
    """反斜杠、百分号和下划线必须按 SQLite LIKE ESCAPE 规则转义。"""
    assert escape_like_pattern(r"a\b%c_d") == r"a\\b\%c\_d"


def test_episode_queries_do_not_expand_like_wildcards(tmp_path: Path) -> None:
    """用户输入 %、_、反斜杠时只能命中字面量目标。"""
    database = Database(tmp_path / "like-escape.db")
    connection = database.open()
    try:
        connection.executemany(
            "INSERT INTO episodes(id,goal,status,started_at,ended_at,reward) VALUES(?,?,?,?,?,?)",
            (
                ("literal-percent", "deploy % service", "success", "2026-01-01", "2026-01-01", 1.0),
                ("literal-underscore", "deploy _ service", "success", "2026-01-01", "2026-01-01", 1.0),
                ("literal-slash", r"deploy \ service", "success", "2026-01-01", "2026-01-01", 1.0),
                ("unrelated", "deploy any service", "success", "2026-01-01", "2026-01-01", 1.0),
            ),
        )
        repository = ExperienceRepository(connection)

        assert [row["id"] for row in repository.list_success_episodes("default", "%", 10)] == ["literal-percent"]
        assert [row["id"] for row in repository.list_success_episodes("default", "_", 10)] == ["literal-underscore"]
        assert [row["id"] for row in repository.list_success_episodes("default", "\\", 10)] == ["literal-slash"]
        assert len(repository.list_success_episodes("default", "", 10)) == 4
    finally:
        database.close()
