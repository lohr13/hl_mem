"""Database and test-harness SQLite ownership contracts."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from hl_mem.storage.database import Database
from tests.support.sqlite_ownership import TestSQLiteOwner


def test_database_context_closes_every_owned_connection(tmp_path: Path) -> None:
    with Database(tmp_path / "owned.db") as database:
        direct = database.open()
        worker = database.open_worker()
        with database.connect() as pooled:
            pooled.execute("SELECT 1")

    for connection in (direct, worker, pooled):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_database_close_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "idempotent.db")
    database.open()
    database.close()
    database.close()


def test_sqlite_factory_fixtures_own_created_resources(
    tmp_path: Path,
    database_factory: Callable[..., Database],
    sqlite_connection_factory: Callable[..., sqlite3.Connection],
) -> None:
    database_factory(tmp_path / "fixture-database.db").open()
    sqlite_connection_factory(tmp_path / "fixture-connection.db").execute("SELECT 1")


def test_sqlite_owner_rejects_live_connection_from_another_thread(
    sqlite_test_owner: TestSQLiteOwner,
) -> None:
    ready = threading.Event()
    probe = threading.Event()
    results: list[int] = []
    errors: list[Exception] = []

    def create_and_probe() -> None:
        try:
            connection = sqlite3.connect(":memory:")
            ready.set()
            if not probe.wait(timeout=5.0):
                raise TimeoutError("main thread did not release SQLite probe")
            results.append(connection.execute("SELECT 1").fetchone()[0])
            connection.close()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=create_and_probe)
    thread.start()
    assert ready.wait(timeout=5.0)

    try:
        with pytest.raises(RuntimeError, match="still open in its creator thread"):
            sqlite_test_owner.close()
    finally:
        probe.set()
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert errors == []
    assert results == [1]
    sqlite_test_owner.close()


def test_sqlite_owner_rejects_untrackable_callable_factory(
    sqlite_test_owner: TestSQLiteOwner,
) -> None:
    def callable_factory(*args: object, **kwargs: object) -> sqlite3.Connection:
        return sqlite3.Connection(*args, **kwargs)

    with pytest.raises(TypeError, match="sqlite3.Connection subclass"):
        sqlite3.connect(":memory:", factory=callable_factory)

    assert sqlite_test_owner.connections == []


@pytest.mark.no_sqlite_autoclose
def test_sqlite_owner_tolerates_connection_closed_by_creator_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = TestSQLiteOwner()
    owner.install(monkeypatch)
    errors: list[Exception] = []

    def create_and_close() -> None:
        try:
            connection = sqlite3.connect(":memory:")
            connection.close()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=create_and_close)
    thread.start()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert errors == []
    owner.close()


@pytest.mark.no_sqlite_autoclose
def test_no_sqlite_autoclose_leaves_resources_unregistered(
    tmp_path: Path,
    sqlite_test_owner: TestSQLiteOwner,
) -> None:
    database = Database(tmp_path / "opt-out.db")
    connection = sqlite3.connect(":memory:")
    try:
        assert sqlite_test_owner.databases == []
        assert sqlite_test_owner.connections == []
    finally:
        database.close()
        connection.close()
