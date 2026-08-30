"""Production runtime SQLite ownership contracts."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.mcp.server import McpMemoryServer
from hl_mem.settings import Settings
from hl_mem.workers.worker import Worker

pytestmark = pytest.mark.no_sqlite_autoclose


def _assert_closed(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_ci_warning_policy_rejects_destructor_time_sqlite_leaks(tmp_path: Path) -> None:
    probe = tmp_path / "test_sqlite_leak_probe.py"
    probe.write_text(
        """\
import gc
import sqlite3
import warnings


class DeliberatelyLeakedConnection(sqlite3.Connection):
    def __del__(self) -> None:
        warnings.warn(f"unclosed database in {self!r}", ResourceWarning)


def test_deliberate_sqlite_leak() -> None:
    connection = sqlite3.connect(":memory:", factory=DeliberatelyLeakedConnection)
    connection.execute("SELECT 1")
    del connection
    gc.collect()
""",
        encoding="utf-8",
    )
    repository_root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            "-m",
            "pytest",
            "-c",
            str(repository_root / "pyproject.toml"),
            str(probe),
            "-q",
            "--tb=short",
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert "PytestUnraisableExceptionWarning" in output
    assert "ResourceWarning: unclosed database" in output


def test_fastapi_lifespan_closes_owned_connections(tmp_path: Path) -> None:
    app = create_app(tmp_path / "api-owner.db")
    connection: sqlite3.Connection | None = None
    try:
        with TestClient(app):
            connection = app.state.db.open()
            connection.execute("SELECT 1")

        assert connection is not None
        _assert_closed(connection)
    finally:
        app.state.db.close()
        if connection is not None:
            connection.close()


def test_mcp_close_closes_owned_connections(tmp_path: Path) -> None:
    server = McpMemoryServer(tmp_path / "mcp-owner.db")
    connection = server.database.open_worker()
    try:
        server.close()
        _assert_closed(connection)
    finally:
        server.database.close()
        connection.close()


def test_worker_close_closes_owned_connections(tmp_path: Path) -> None:
    settings = replace(Settings.for_test(), database_path=str(tmp_path / "worker-owner.db"))
    worker = Worker(settings)
    connection = worker.connection
    try:
        worker.close()
        _assert_closed(connection)
    finally:
        if worker.database is not None:
            worker.database.close()
        connection.close()
