from __future__ import annotations

import pytest

from hl_mem.application.ingest import IngestService
from hl_mem.errors import ConflictError, ValidationError
from hl_mem.mcp.server import McpMemoryServer, get_tool_schemas
from hl_mem.storage.database import Database


def _counts(connection) -> tuple[int, int]:
    events = connection.execute("SELECT count(*) FROM events").fetchone()[0]
    jobs = connection.execute("SELECT count(*) FROM jobs WHERE job_type='extract_event'").fetchone()[0]
    return int(events), int(jobs)


def test_explicit_save_reuses_same_key_and_payload_in_namespace(tmp_path) -> None:
    connection = Database(tmp_path / "explicit.db").open()
    service = IngestService(connection)

    first = service.save_explicit_memory(
        "记住 SQLite",
        "项目",
        qualifiers={"source": "test"},
        idempotency_key="save-1",
        namespace="project-a",
    )
    duplicate = service.save_explicit_memory(
        "记住 SQLite",
        "项目",
        qualifiers={"source": "test"},
        idempotency_key="save-1",
        namespace="project-a",
    )

    assert first["created"] is True
    assert duplicate == {"id": first["id"], "created": False}
    assert _counts(connection) == (1, 1)
    assert connection.execute("SELECT tenant_id FROM events WHERE id=?", (first["id"],)).fetchone()[0] == "project-a"


def test_explicit_save_rejects_same_key_with_different_payload(tmp_path) -> None:
    connection = Database(tmp_path / "conflict.db").open()
    service = IngestService(connection)
    first = service.save_explicit_memory(
        "第一版",
        idempotency_key="save-conflict",
        namespace="project-a",
    )

    with pytest.raises(ConflictError, match="different event payload"):
        service.save_explicit_memory(
            "第二版",
            idempotency_key="save-conflict",
            namespace="project-a",
        )

    assert _counts(connection) == (1, 1)
    assert connection.execute("SELECT id FROM events").fetchone()[0] == first["id"]


def test_explicit_save_without_key_preserves_create_each_time_semantics(tmp_path) -> None:
    connection = Database(tmp_path / "unkeyed.db").open()
    service = IngestService(connection)

    first = service.save_explicit_memory("重复内容")
    second = service.save_explicit_memory("重复内容")

    assert first["created"] is True
    assert second["created"] is True
    assert first["id"] != second["id"]
    assert _counts(connection) == (2, 2)


def test_ingest_accepts_namespace_alias_and_rejects_ambiguous_aliases(tmp_path) -> None:
    connection = Database(tmp_path / "namespace.db").open()
    service = IngestService(connection)

    result = service.ingest_event(
        {
            "namespace": "project-a",
            "event_type": "message",
            "actor_type": "user",
            "content": {"text": "hello"},
        }
    )

    assert connection.execute("SELECT tenant_id FROM events WHERE id=?", (result["id"],)).fetchone()[0] == "project-a"
    with pytest.raises(ValidationError, match="must match"):
        service.ingest_event(
            {
                "namespace": "project-a",
                "tenant_id": "project-b",
                "event_type": "message",
                "actor_type": "user",
                "content": {"text": "hello"},
            }
        )


def test_mcp_memory_save_contract_and_true_created_state(tmp_path) -> None:
    schema = next(tool for tool in get_tool_schemas() if tool["name"] == "memory_save")
    properties = schema["inputSchema"]["properties"]
    assert properties["idempotency_key"]["minLength"] == 1
    assert properties["idempotency_key"]["maxLength"] == 200
    assert properties["namespace"]["maxLength"] == 100

    server = McpMemoryServer(tmp_path / "mcp.db")
    arguments = {
        "text": "记住 SQLite",
        "subject": "项目",
        "namespace": "project-a",
        "idempotency_key": "mcp-save-1",
    }
    first = server.call_tool("memory_save", arguments)
    duplicate = server.call_tool("memory_save", arguments)

    assert first["created"] is True
    assert duplicate == {"id": first["id"], "created": False}
    with server.database.connect() as connection:
        assert _counts(connection) == (1, 1)
        assert connection.execute("SELECT tenant_id FROM events").fetchone()[0] == "project-a"


def test_mcp_memory_save_rejects_conflict_and_oversized_key(tmp_path) -> None:
    server = McpMemoryServer(tmp_path / "mcp-conflict.db")
    server.call_tool(
        "memory_save",
        {"text": "第一版", "idempotency_key": "mcp-conflict"},
    )

    with pytest.raises(ConflictError, match="different event payload"):
        server.call_tool(
            "memory_save",
            {"text": "第二版", "idempotency_key": "mcp-conflict"},
        )
    with pytest.raises(ValueError, match="at most 200"):
        server.call_tool(
            "memory_save",
            {"text": "超长键", "idempotency_key": "x" * 201},
        )
    with pytest.raises(ValueError, match="must not be empty"):
        server.call_tool(
            "memory_save",
            {"text": "空键", "idempotency_key": ""},
        )
    with pytest.raises(ValueError, match="namespace must be at most 100"):
        server.call_tool(
            "memory_save",
            {"text": "超长 namespace", "namespace": "n" * 101},
        )
