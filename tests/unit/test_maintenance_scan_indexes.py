"""生命周期维护扫描索引的 migration 回归测试。"""

from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database


def test_ttl_and_temporal_cleanup_scan_indexes_exist(tmp_path: Path) -> None:
    """新数据库必须应用 TTL 与 temporal cleanup 复合索引。"""
    database = Database(tmp_path / "maintenance-indexes.db")
    connection = database.open()
    try:
        indexes = {
            str(row["name"]): bool(row["partial"])
            for row in connection.execute("PRAGMA index_list('claims')").fetchall()
        }
        assert indexes["idx_claims_expires_scan"] is True
        assert indexes["idx_claims_temporal_cleanup"] is False
        migrations = {
            str(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations WHERE version IN (?,?)",
                ("028_relation_proposals_drop_mode_unique", "029_ttl_scan_indexes"),
            ).fetchall()
        }
        assert migrations == {
            "028_relation_proposals_drop_mode_unique",
            "029_ttl_scan_indexes",
        }
    finally:
        database.close()
