"""无传输耦合的 MCP 工具契约实现。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem.lifecycle import assert_transition
from hl_mem.recall.recall_pipeline import stale_observations
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository, EvidenceRepository, EventRepository, JobRepository


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
        with self.database.connect() as connection:
            if name == "memory_save":
                return self._save(connection, arguments)
            if name == "memory_recall":
                return self._recall(connection, arguments)
            if name == "memory_forget":
                return self._forget(connection, arguments)
            return self._explain(connection, arguments)

    @staticmethod
    def _save(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """原子保存显式记忆事件并创建提取任务。"""
        now = datetime.now(timezone.utc).isoformat()
        event_id = uuid.uuid4().hex
        content_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            EventRepository(connection).insert_event(
                {
                    "id": event_id,
                    "event_type": "explicit_memory",
                    "actor_type": "user",
                    "content_json": content_json,
                    "occurred_at": now,
                    "recorded_at": now,
                    "content_hash": hashlib.sha256(content_json.encode()).hexdigest(),
                },
                commit=False,
            )
            JobRepository(connection).insert_job(
                {
                    "id": uuid.uuid4().hex,
                    "job_type": "extract_event",
                    "payload_json": json.dumps({"event_id": event_id}),
                    "idempotency_key": f"extract:{event_id}",
                    "created_at": now,
                    "updated_at": now,
                },
                commit=False,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {"id": event_id, "created": True}

    @staticmethod
    def _recall(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过正式 FTS 索引召回活跃 claim。"""
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", 20))
        claims = ClaimRepository(connection).search_claims_fts(query, limit)
        results = [
            {"id": claim["id"], "value_json": claim["value_json"], "status": claim["status"]}
            for claim in claims
        ]
        return {"results": results, "total": len(results)}

    @staticmethod
    def _forget(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过生命周期守卫撤回 claim 并清除其向量。"""
        memory_id = str(arguments.get("id", ""))
        repository = ClaimRepository(connection)
        claim = repository.get_claim(memory_id)
        if not claim:
            return {"id": memory_id, "forgotten": False}
        assert_transition(claim["status"], "retracted")
        forgotten = repository.retract(memory_id)
        if forgotten:
            stale_observations(connection, memory_id)
        return {"id": memory_id, "forgotten": forgotten}

    @staticmethod
    def _explain(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过 repository 返回事件或 claim 的证据链。"""
        memory_id = str(arguments.get("id", ""))
        event = EventRepository(connection).get_event(memory_id)
        if event:
            return {"type": "event", "id": memory_id, "evidence": [{"type": "event", "id": memory_id}]}
        claim = ClaimRepository(connection).get_claim(memory_id)
        if not claim:
            raise ValueError(f"memory not found: {memory_id}")
        links = EvidenceRepository(connection).get_links_for_derived("claim", memory_id)
        evidence = [
            {
                "evidence_type": link["evidence_type"],
                "evidence_id": link["evidence_id"],
                "relation": link["relation"],
            }
            for link in links
        ]
        return {"type": "claim", "id": memory_id, "evidence": evidence}
