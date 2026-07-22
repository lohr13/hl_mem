"""无传输耦合的 MCP 工具契约实现。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.storage.database import Database
from hl_mem.storage.repository import EventRepository


class McpMemoryServer:
    """提供可嵌入任意 MCP 传输层的最小记忆工具集。"""

    _TOOLS = ("memory_recall", "memory_save", "memory_forget", "memory_explain")

    def __init__(self, database_path: str | Path) -> None:
        self.database = Database(database_path)

    def list_tools(self) -> tuple[str, ...]:
        """返回稳定的 MCP 工具名称。"""
        return self._TOOLS

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用一个记忆工具并返回 JSON 可序列化结果。"""
        if name not in self._TOOLS:
            raise ValueError(f"unknown MCP tool: {name}")
        connection = self.database.open()
        if name == "memory_save":
            now = datetime.now(timezone.utc).isoformat()
            event_id = uuid.uuid4().hex
            content_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
            EventRepository(connection).insert_event(
                {
                    "id": event_id,
                    "event_type": "explicit_memory",
                    "actor_type": "user",
                    "content_json": content_json,
                    "occurred_at": now,
                    "recorded_at": now,
                    "content_hash": hashlib.sha256(content_json.encode()).hexdigest(),
                }
            )
            return {"id": event_id, "created": True}
        memory_id = str(arguments.get("id", ""))
        if name == "memory_explain":
            event = EventRepository(connection).get_event(memory_id)
            if event:
                return {"type": "event", "id": memory_id, "evidence": [{"type": "event", "id": memory_id}]}
            row = connection.execute("SELECT * FROM claims WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise ValueError(f"memory not found: {memory_id}")
            evidence = connection.execute(
                "SELECT evidence_type,evidence_id,relation FROM evidence_links WHERE derived_id=?", (memory_id,)
            ).fetchall()
            return {"type": "claim", "id": memory_id, "evidence": [dict(item) for item in evidence]}
        if name == "memory_forget":
            cursor = connection.execute("UPDATE claims SET status='retracted' WHERE id=?", (memory_id,))
            connection.commit()
            return {"id": memory_id, "forgotten": cursor.rowcount == 1}
        query = str(arguments.get("query", ""))
        rows = connection.execute(
            "SELECT id,value_json,status FROM claims WHERE status='active' AND value_json LIKE ? LIMIT ?",
            (f"%{query}%", int(arguments.get("limit", 20))),
        ).fetchall()
        return {"results": [dict(row) for row in rows], "total": len(rows)}
