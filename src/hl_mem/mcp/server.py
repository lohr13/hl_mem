"""无传输耦合的 MCP 工具契约实现。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hl_mem import components
from hl_mem.application.correction import CorrectionService
from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService
from hl_mem.application.memories import MemoryQueryService
from hl_mem.application.recall import RecallService
from hl_mem.experience.service import ExperienceService
from hl_mem.settings import Settings
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from hl_mem.storage.events import EventRepository
from hl_mem.storage.evidence import EvidenceRepository


def _context_packet_schema() -> dict[str, Any]:
    """返回与 REST DTO 一致的严格 Context Packet v1 JSON Schema。"""
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {
                "type": "string",
                "enum": ["claim", "observation", "policy", "episode", "trace"],
            },
            "id": {"type": "string"},
            "text": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "feedback_id": {
                "type": "string",
                "minLength": 1,
                "pattern": r".*\S.*",
            },
        },
        "required": ["type", "id", "text", "evidence", "feedback_id"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_major": {"type": "integer", "const": 1},
            "schema_minor": {"type": "integer", "const": 0},
            "query_id": {"type": "string"},
            "answerability": {
                "type": "string",
                "enum": ["supported", "low_confidence", "no_evidence"],
            },
            "feedback_state": {
                "type": "string",
                "enum": ["available", "degraded"],
            },
            "items": {"type": "array", "items": item_schema},
            "used_tokens_estimate": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
        },
        "required": [
            "schema_major",
            "schema_minor",
            "query_id",
            "answerability",
            "feedback_state",
            "items",
            "used_tokens_estimate",
            "truncated",
        ],
    }


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
                    "token_budget": {"type": "integer", "minimum": 1},
                    "context_mode": {"type": "string", "enum": ["packed"]},
                    "response_format": {
                        "type": "string",
                        "enum": ["legacy", "context_packet", "both"],
                        "default": "legacy",
                    },
                    "namespace": {"type": "string"},
                    "session_id": {"type": "string"},
                    "debug": {"type": "boolean"},
                },
                "required": ["query"],
            },
            "outputSchema": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "context_packet": _context_packet_schema(),
                    "results": {"type": "array"},
                    "observations": {"type": "array"},
                    "policies": {"type": "array"},
                    "total": {"type": "integer"},
                    "query_id": {"type": "string"},
                    "answerability": {
                        "type": "string",
                        "enum": ["supported", "low_confidence", "no_evidence"],
                    },
                    "context": {"type": "object"},
                    "search_trace": {"type": "object"},
                },
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
                    "idempotency_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "namespace": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                    },
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
            "name": "memory_get",
            "description": "Get complete claim details by identifier.",
            "inputSchema": {
                **object_schema,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_correct",
            "description": "Replace only a claim's content and preserve its classification.",
            "inputSchema": {
                **object_schema,
                "properties": {
                    "id": {"type": "string"},
                    "corrected_text": {"type": "string", "minLength": 1, "maxLength": 50000},
                    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "required": ["id", "corrected_text", "idempotency_key"],
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
        """使用已加载的统一配置创建 MCP 服务。"""
        if not isinstance(settings, Settings):
            settings = replace(Settings(), database_path=str(settings))
        components.initialize_process(settings)
        self.database = Database(settings=settings)
        self.settings = settings
        self.embedder = embedder or components.make_embedder(settings)
        self.reranker = reranker if reranker is not None else components.make_reranker(settings)

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
            if name == "memory_get":
                return self._get(connection, arguments)
            if name == "memory_correct":
                return self._correct(connection, arguments)
            if name == "memory_feedback":
                return self._feedback(connection, arguments)
            return self._explain(connection, arguments)

    def _save(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """原子保存显式记忆事件并创建提取任务。"""
        text = str(arguments.get("text") or arguments.get("content") or "")
        if not text:
            raise ValueError("text or content is required")
        idempotency_key = arguments.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise ValueError("idempotency_key must be a string")
            if not idempotency_key:
                raise ValueError("idempotency_key must not be empty")
            if len(idempotency_key) > 200:
                raise ValueError("idempotency_key must be at most 200 characters")
        namespace = arguments.get("namespace", "default")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace must be a non-empty string")
        if len(namespace) > 100:
            raise ValueError("namespace must be at most 100 characters")
        result = IngestService(connection).save_explicit_memory(
            text,
            str(arguments.get("subject", "用户")),
            str(arguments.get("predicate", "explicit_memory")),
            arguments.get("qualifiers") or {},
            idempotency_key=idempotency_key,
            namespace=namespace,
        )
        return result

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
            query=query,
            limit=limit,
            as_of=arguments.get("as_of"),
            intent=arguments.get("intent"),
            known_as_of=arguments.get("known_as_of"),
            query_id=arguments.get("query_id"),
            token_budget=(int(arguments["token_budget"]) if arguments.get("token_budget") is not None else None),
            context_mode=arguments.get("context_mode"),
            response_format=str(arguments.get("response_format", "legacy")),
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
        memory_id = correction.get("memory_id")
        action = correction.get("action")
        key = correction.get("idempotency_key")
        if (
            not isinstance(memory_id, str)
            or not memory_id
            or action not in {"retract", "replace"}
            or not isinstance(key, str)
            or not key
        ):
            raise ValueError("correction requires memory_id, retract|replace action, and idempotency_key")
        correction_result = CorrectionService(connection, self.embedder, settings=self.settings).apply(
            memory_id,
            action=action,
            corrected_text=correction.get("corrected_text"),
            idempotency_key=key,
        )
        result["correction"] = correction_result
        result["correction_event_id"] = correction_result["correction_event_id"]
        return result

    @staticmethod
    def _get(connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """复用共享查询服务返回完整 Claim 详情。"""
        return MemoryQueryService(connection).get_memory(str(arguments.get("id", "")))

    def _correct(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """复用共享纠正服务执行仅内容替换。"""
        return CorrectionService(connection, self.embedder, settings=self.settings).correct(
            arguments.get("id"),
            arguments.get("corrected_text"),
            arguments.get("idempotency_key"),
        )

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
