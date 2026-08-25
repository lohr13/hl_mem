"""MCP SDK transport runtime contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from hl_mem import __version__
from hl_mem.daily_cli import OFFLINE_CONFIG
from hl_mem.mcp.server import McpMemoryServer, get_tool_schemas
from hl_mem.settings import Settings

mcp = pytest.importorskip("mcp")
Client = mcp.Client
MCPError = pytest.importorskip("mcp.shared.exceptions").MCPError
ROOT = Path(__file__).resolve().parents[2]


def test_mcp_runtime_module_is_available() -> None:
    assert importlib.util.find_spec("hl_mem.mcp.runtime") is not None


@pytest.mark.asyncio
async def test_in_memory_protocol_reuses_schemas_and_returns_tool_results(tmp_path: Path) -> None:
    from hl_mem.mcp.runtime import create_mcp_server

    memory = McpMemoryServer(replace(Settings.for_test(), database_path=str(tmp_path / "mcp.db")))
    try:
        async with Client(create_mcp_server(memory)) as client:
            listed = await client.list_tools()
            assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools] == get_tool_schemas()

            saved = await client.call_tool(
                "memory_save",
                {"text": "记住 MCP stdio", "subject": "项目"},
            )
            assert saved.is_error is False
            assert saved.structured_content["id"]
            assert json.loads(saved.content[0].text)["id"] == saved.structured_content["id"]

            invalid = await client.call_tool("memory_save", {})
            assert invalid.is_error is True
            assert "text or content is required" in invalid.content[0].text
    finally:
        memory.database.close()


@pytest.mark.asyncio
async def test_sync_business_call_runs_in_worker_thread() -> None:
    from hl_mem.mcp.runtime import create_mcp_server

    event_loop_thread = threading.get_ident()

    class RecordingMemoryServer:
        def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
            return {"name": name, "thread_id": threading.get_ident()}

    async with Client(create_mcp_server(RecordingMemoryServer())) as client:
        result = await client.call_tool("memory_recall", {"query": "thread"})

    assert result.is_error is False
    assert result.structured_content["name"] == "memory_recall"
    assert result.structured_content["thread_id"] != event_loop_thread


@pytest.mark.asyncio
async def test_unexpected_backend_error_remains_a_protocol_error() -> None:
    from hl_mem.mcp.runtime import create_mcp_server

    class BrokenMemoryServer:
        def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
            raise TypeError("unexpected backend failure")

    async with Client(create_mcp_server(BrokenMemoryServer())) as client:
        with pytest.raises(MCPError, match="Internal server error"):
            await client.call_tool("memory_recall", {"query": "broken"})


def test_entry_point_loads_explicit_config_env_and_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hl_mem.mcp import runtime

    config_path = tmp_path / "config.toml"
    config_path.write_text(OFFLINE_CONFIG, encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    database_path = tmp_path / "runtime.db"
    captured: dict[str, object] = {}

    async def fake_run_stdio(memory: McpMemoryServer) -> None:
        captured["settings"] = memory.settings

    monkeypatch.setattr(runtime, "run_stdio", fake_run_stdio)

    runtime.main(
        [
            "--config",
            str(config_path),
            "--env-file",
            str(env_path),
            "--db",
            str(database_path),
        ]
    )

    settings = captured["settings"]
    assert isinstance(settings, Settings)
    assert settings.database_path == str(database_path)
    assert settings.recall_dense_enabled is False


def test_entry_point_rejects_unknown_arguments() -> None:
    from hl_mem.mcp.runtime import main

    with pytest.raises(SystemExit, match="2"):
        main(["--transport", "http"])


def test_packaged_console_and_module_entry_points(tmp_path: Path) -> None:
    entry = next(item for item in entry_points(group="console_scripts") if item.name == "hl-mem-mcp")
    assert entry.value == "hl_mem.mcp.runtime:main"

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "hl_mem.mcp", "--version"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"hl_mem MCP {__version__}"
    assert "hl_mem MCP" not in completed.stderr
