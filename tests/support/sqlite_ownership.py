"""Deterministic SQLite resource ownership for pytest."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from functools import wraps
from pathlib import Path

import pytest

from hl_mem.settings import Settings
from hl_mem.storage.database import Database

_THREAD_AFFINITY_ERROR = "SQLite objects created in a thread can only be used in that same thread"


class TestSQLiteOwner:
    """Own SQLite resources created during one test."""

    __test__ = False

    def __init__(self) -> None:
        self.databases: list[Database] = []
        self.connections: list[sqlite3.Connection] = []
        self._database_init: Callable[..., None] = Database.__init__
        self._sqlite_connect: Callable[..., sqlite3.Connection] = sqlite3.connect
        self._installed = False

    def database(self, path: Path, *, settings: Settings | None = None, **kwargs: object) -> Database:
        database = Database(path, settings=settings, **kwargs)
        self._register_database(database)
        return database

    def connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = sqlite3.connect(*args, **kwargs)
        self._register_connection(connection)
        return connection

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if self._installed:
            return
        self._database_init = Database.__init__
        self._sqlite_connect = sqlite3.connect

        @wraps(self._database_init)
        def database_init(database: Database, *args: object, **kwargs: object) -> None:
            self._database_init(database, *args, **kwargs)
            self._register_database(database)

        @wraps(self._sqlite_connect)
        def sqlite_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = self._sqlite_connect(*args, **kwargs)
            self._register_connection(connection)
            return connection

        monkeypatch.setattr(Database, "__init__", database_init)
        monkeypatch.setattr(sqlite3, "connect", sqlite_connect)
        self._installed = True

    def close(self) -> None:
        for database in reversed(self.databases):
            database.close()
        for connection in reversed(self.connections):
            try:
                connection.close()
            except sqlite3.ProgrammingError as error:
                # SQLite checks creator-thread affinity even after that thread closed the connection.
                if _THREAD_AFFINITY_ERROR not in str(error):
                    raise

    def _register_database(self, database: Database) -> None:
        if not any(owned is database for owned in self.databases):
            self.databases.append(database)

    def _register_connection(self, connection: sqlite3.Connection) -> None:
        if not any(owned is connection for owned in self.connections):
            self.connections.append(connection)
