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
        self._closed_connections: list[sqlite3.Connection] = []
        self._tracking_factories: dict[type[sqlite3.Connection], type[sqlite3.Connection]] = {}
        self._installed = False

    def database(self, path: Path, *, settings: Settings | None = None, **kwargs: object) -> Database:
        database = Database(path, settings=settings, **kwargs)
        self._register_database(database)
        return database

    def connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        return self._connect(*args, **kwargs)

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
            return self._connect(*args, **kwargs)

        monkeypatch.setattr(Database, "__init__", database_init)
        monkeypatch.setattr(sqlite3, "connect", sqlite_connect)
        self._installed = True

    def close(self) -> None:
        for database in reversed(self.databases):
            database.close()
        for connection in reversed(self.connections):
            if any(closed is connection for closed in self._closed_connections):
                continue
            try:
                connection.close()
            except sqlite3.ProgrammingError as error:
                if _THREAD_AFFINITY_ERROR in str(error):
                    raise RuntimeError(
                        "SQLite connection is still open in its creator thread; close it there before test teardown"
                    ) from error
                raise

    def _register_database(self, database: Database) -> None:
        if not any(owned is database for owned in self.databases):
            self.databases.append(database)

    def _register_connection(self, connection: sqlite3.Connection) -> None:
        if not any(owned is connection for owned in self.connections):
            self.connections.append(connection)

    def _connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        connect_args = list(args)
        connect_kwargs = dict(kwargs)
        factory = connect_args[5] if len(connect_args) > 5 else connect_kwargs.get("factory", sqlite3.Connection)
        if not isinstance(factory, type) or not issubclass(factory, sqlite3.Connection):
            raise TypeError("TestSQLiteOwner requires a sqlite3.Connection subclass factory")
        tracking_factory = self._tracking_factory(factory)
        if len(connect_args) > 5:
            connect_args[5] = tracking_factory
        else:
            connect_kwargs["factory"] = tracking_factory
        connection = self._sqlite_connect(*connect_args, **connect_kwargs)
        self._register_connection(connection)
        return connection

    def _tracking_factory(self, factory: type[sqlite3.Connection]) -> type[sqlite3.Connection]:
        tracking_factory = self._tracking_factories.get(factory)
        if tracking_factory is not None:
            return tracking_factory

        def close(connection: sqlite3.Connection) -> None:
            factory.close(connection)
            if not any(closed is connection for closed in self._closed_connections):
                self._closed_connections.append(connection)

        tracking_factory = type(
            factory.__name__,
            (factory,),
            {"__module__": factory.__module__, "close": close},
        )
        self._tracking_factories[factory] = tracking_factory
        return tracking_factory
