"""无传输耦合的 MCP 工具契约实现。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import components
from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository


def get_tool_schemas() -> list[dict[str, Any]]:
    """返回稳定、可快照化的 MCP 工具 JSON Schema。"""
    object_schema = {"type": "object", "additionalProperties": True}
    return [
        {
            "name": "memory_recall",
            "description": "Recall relevant memories.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "as_of": {"type": "string"},
                    "known_as_of": {"type": "string"},
                    "intent": {"type": "string"},
                    "namespace": {"type": "string"},
                    "session_id": {"type": "string"},
                    "debug": {"type": "boolean"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_save",
            "description": "Save an explicit memory.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "text": {"type": "string"},
                    "content": {"type": "string"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "qualifiers": {"type": "object"},
                },
            },
        },
        {
            "name": "memory_forget",
            "description": "Forget a memory by identifier.",
            "inputSchema": {
                **object_schema,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_explain",
            "description": "Explain a memory's evidence chain.",
            "inputSchema": {
                **object_schema,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_feedback",
            "description": "Submit usefulness feedback.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "feedback_id": {"type": "string"},
                    "helpful": {"type": "boolean"},
                    "task_outcome": {"type": "number", "minimum": 0, "maximum": 1},
                    "correction": {"type": "object"},
                },
                "required": ["feedback_id", "helpful"],
            },
        },
    ]


class McpMemoryServer:
    """提供可嵌入任意 MCP 传输层的最小记忆工具集。"""

    _TOOLS = tuple(tool["name"] for tool in get_tool_schemas())

    def __init__(
        self,
        settings: Settings | str | Path,
        embedder: Any = None,
        reranker: Any = None,
    ) -> None:
        """使用统一配置创建 MCP 服务，并兼容旧的数据库路径入口。"""
        if isinstance(settings, Settings):
            resolved_settings = settings
        else:
            from dataclasses import replace

            resolved_settings = replace(Settings.from_env(), database_path=str(settings))
        self.database = Database(resolved_settings.database_path)
        self.settings = resolved_settings
        self.embedder = embedder or components.make_embedder(resolved_settings)
        self.reranker = reranker if reranker is not None else components.make_reranker(resolved_settings)

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
            if name == "memory_feedback":
                return self._feedback(connection, arguments)
            return self._explain(connection, arguments)

    def _save(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """原子保存显式记忆事件并创建提取任务。"""
        text = str(arguments.get("text") or arguments.get("content") or "")
        if not text:
            raise ValueError("text or content is required")
        result = IngestService(connection).save_explicit_memory(
            text,
            str(arguments.get("subject", "用户")),
            str(arguments.get("predicate", "explicit_memory")),
            arguments.get("qualifiers") or {},
        )
        return {**result, "created": True}

    def _recall(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过共享召回服务执行混合召回。"""
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", self.settings.recall_default_limit))
        return RecallService(
            connection,
            self.embedder,
            self.reranker,
            settings=self.settings,
            query_expander=components.make_query_expander(self.settings, connection),
        ).recall(
            query,
            limit,
            arguments.get("as_of"),
            arguments.get("intent"),
            arguments.get("known_as_of"),
            arguments.get("query_id"),
            namespace=str(arguments.get("namespace", "default")),
            session_id=arguments.get("session_id"),
            debug=bool(arguments.get("debug", False)),
        )

    @staticmethod
    def _forget(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过生命周期守卫撤回 claim 并清除其向量。"""
        memory_id = str(arguments.get("id", ""))
        try:
            return ForgetService(connection).forget(memory_id)
        except ValueError as error:
            if not str(error).startswith("memory not found"):
                raise
            return {"id": memory_id, "forgotten": False}

    def _feedback(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 feedback_id 提交 usefulness，并仅在显式 correction 字段存在时纠正记忆。"""
        feedback_id = str(arguments.get("feedback_id", ""))
        if not feedback_id:
            raise ValueError("feedback_id is required")
        now = datetime.now(timezone.utc).isoformat()
        result: dict[str, Any] = ExperienceService(connection, settings=self.settings).submit_retrieval_feedback(
            feedback_id,
            bool(arguments["helpful"]),
            float(arguments["task_outcome"]) if arguments.get("task_outcome") is not None else None,
            now,
        )
        correction = arguments.get("correction")
        if not correction:
            return result
        memory_id = str(correction.get("memory_id", ""))
        action = str(correction.get("action", ""))
        key = str(correction.get("idempotency_key", ""))
        if not memory_id or action not in {"retract", "replace"} or not key:
            raise ValueError("correction requires memory_id, retract|replace action, and idempotency_key")
        text = str(correction.get("corrected_text") or "")
        if action == "replace" and not text:
            raise ValueError("corrected_text is required for replace")
        event = IngestService(connection).ingest_event(
            {
                "idempotency_key": key,
                "tenant_id": "default",
                "event_type": "feedback" if action == "retract" else "correction",
                "actor_type": "user",
                "content": {"memory_id": memory_id, "action": action, "text": text},
                "occurred_at": now,
            },
            key,
        )
        if not event["created"]:
            result["correction"] = {"id": memory_id, "idempotent": True}
        elif action == "retract":
            result["correction"] = ForgetService(connection).forget(memory_id)
        else:
            result["correction"] = {
                "id": memory_id,
                "replacement_event_id": event["id"],
            }
        return result

    @staticmethod
    def _explain(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过 repository 返回事件或 claim 的证据链。"""
        memory_id = str(arguments.get("id", ""))
        event = EventRepository(connection).get_event(memory_id)
        if event:
            return {
                "type": "event",
                "id": memory_id,
                "evidence": [{"type": "event", "id": memory_id}],
            }
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
