from __future__ import annotations

from pathlib import Path

from hl_mem.storage.database import Database

MIGRATION_DIRECTORY = Path(__file__).parents[2] / "src" / "hl_mem" / "storage" / "migrations"
DATA_MIGRATION_VERSIONS = (
    "006_data_conflict_key_v2",
    "011_data_fact_hash_v2",
    "016_data_conflict_key_v3",
    "038_data_subject_canonicalization_v2",
)


def _expected_versions() -> tuple[str, ...]:
    sql_versions = (path.stem for path in MIGRATION_DIRECTORY.glob("*.sql"))
    return tuple(sorted((*sql_versions, *DATA_MIGRATION_VERSIONS)))


def test_empty_database_applies_every_immutable_sql_migration(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    connection = database.open()
    applied = tuple(row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version"))

    assert applied == _expected_versions()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    database.close()


def test_current_database_reopen_applies_no_migration_twice(tmp_path: Path) -> None:
    path = tmp_path / "repeat.db"
    first_database = Database(path)
    first = first_database.open()
    versions = tuple(row[0] for row in first.execute("SELECT version FROM schema_migrations ORDER BY version"))
    first_database.close()

    reopened_database = Database(path)
    reopened = reopened_database.open()

    assert (
        tuple(row[0] for row in reopened.execute("SELECT version FROM schema_migrations ORDER BY version")) == versions
    )
    assert reopened.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(set(versions))
    assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened_database.close()
