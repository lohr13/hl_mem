import json

import pytest

from hl_mem.cli import export_database, import_database, main
from hl_mem.mcp.server import McpMemoryServer
from hl_mem.storage.database import Database


def test_mcp_exposes_minimal_memory_tool_contract(tmp_path) -> None:
    server = McpMemoryServer(tmp_path / "mcp.db")
    assert set(server.list_tools()) == {"memory_recall", "memory_save", "memory_forget", "memory_explain"}
    saved = server.call_tool("memory_save", {"text": "记住 SQLite", "subject": "项目"})
    assert saved["id"]
    assert server.call_tool("memory_explain", {"id": saved["id"]})["type"] == "event"


def test_cli_export_import_round_trip(tmp_path) -> None:
    source = tmp_path / "source.db"
    connection = Database(source).open()
    connection.execute(
        "INSERT INTO events(id,event_type,actor_type,content_json,occurred_at,recorded_at) VALUES (?,?,?,?,?,?)",
        ("e1", "message", "user", json.dumps({"text": "中文"}, ensure_ascii=False), "2026-01-01", "2026-01-01"),
    )
    connection.commit()
    archive = tmp_path / "memory.jsonl"
    assert export_database(source, archive) == 1
    target = tmp_path / "target.db"
    assert import_database(target, archive) == 1
    assert Database(target).open().execute("SELECT content_json FROM events WHERE id='e1'").fetchone()[0]


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--version"])

    assert capsys.readouterr().out == "hl_mem 0.2.0\n"
