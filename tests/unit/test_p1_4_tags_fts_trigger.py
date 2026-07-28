"""P1-4：tags FTS 更新触发器字段范围测试。"""

from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database


def test_tags_fts_trigger_only_tracks_topic_tags_changes(tmp_path: Path) -> None:
    """访问计数更新不得触发 tags FTS 写放大，标签更新必须同步索引。"""
    database = Database(tmp_path / "tags-trigger.db")
    connection = database.open()
    try:
        connection.execute(
            "INSERT INTO claims(id,status,recorded_from,topic_tags_json) VALUES(?,?,?,?)",
            ("claim", "active", "2026-01-01T00:00:00+00:00", '["before"]'),
        )
        baseline = connection.total_changes
        connection.execute(
            "UPDATE claims SET access_count=access_count+1 WHERE id=?", ("claim",)
        )
        access_delta = connection.total_changes - baseline

        baseline = connection.total_changes
        connection.execute(
            "UPDATE claims SET topic_tags_json=? WHERE id=?", ('["after"]', "claim")
        )
        tags_delta = connection.total_changes - baseline

        assert access_delta == 1
        assert tags_delta > 1
        indexed = connection.execute(
            "SELECT c.id FROM claims_tags_fts f JOIN claims c ON c.rowid=f.rowid "
            "WHERE claims_tags_fts MATCH ?",
            ('"after"',),
        ).fetchone()
        assert indexed["id"] == "claim"
    finally:
        database.close()
