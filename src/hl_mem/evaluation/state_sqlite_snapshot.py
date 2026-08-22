"""Read-only SQLite snapshot primitives shared by state evaluators."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


def open_readonly_database(database_path: str | Path) -> sqlite3.Connection:
    """Open an existing database in URI read-only and query-only mode."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return declared column names without interpreting their semantics."""

    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


@contextmanager
def readonly_snapshot(
    database_path: str | Path,
    *,
    opener: Callable[[str | Path], sqlite3.Connection] = open_readonly_database,
) -> Iterator[sqlite3.Connection]:
    """Hold exactly one read transaction for all queries in a snapshot."""

    connection = opener(database_path)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
