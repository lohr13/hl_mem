"""vector index 控制 schema 的 SQL migration 回归测试。"""

from pathlib import Path

from hl_mem.storage.database import Database


def test_migration_037_installs_control_tables_and_dirty_triggers(tmp_path: Path) -> None:
    """普通 SQL migration 应独立于可选 sqlite-vec 后端安装控制 schema。"""
    database = Database(tmp_path / "migration-037.db")
    connection = database.open()
    try:
        version = connection.execute(
            "SELECT version FROM schema_migrations WHERE version='037_vector_index_control'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('vector_index_state','claim_vector_dirty')"
            ).fetchall()
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'claim_vector_dirty_a%'"
            ).fetchall()
        }

        assert version is not None
        assert tables == {"vector_index_state", "claim_vector_dirty"}
        assert triggers == {"claim_vector_dirty_ai", "claim_vector_dirty_au", "claim_vector_dirty_ad"}
    finally:
        database.close()


def test_migration_037_repairs_legacy_marker_without_triggers(tmp_path: Path) -> None:
    """旧 Python 037 已登记但未安装 trigger 时，应重放幂等 SQL 修复缺失对象。"""
    database_path = tmp_path / "legacy-python-037.db"
    initial = Database(database_path)
    connection = initial.open()
    for trigger in ("claim_vector_dirty_ai", "claim_vector_dirty_au", "claim_vector_dirty_ad"):
        connection.execute(f"DROP TRIGGER {trigger}")
    connection.commit()
    initial.close()

    upgraded = Database(database_path)
    repaired = upgraded.open()
    try:
        triggers = {
            row[0]
            for row in repaired.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'claim_vector_dirty_a%'"
            ).fetchall()
        }
        assert triggers == {"claim_vector_dirty_ai", "claim_vector_dirty_au", "claim_vector_dirty_ad"}
    finally:
        upgraded.close()
