"""无传输耦合的 MCP 工具契约实现。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hl_mem.application.forget import ForgetService
from hl_mem.application.ingest import IngestService
from hl_mem.application.recall import RecallService
from hl_mem.settings import Settings
from hl_mem.storage.database import Database
from hl_mem.storage.repository import ClaimRepository, EvidenceRepository, EventRepository


class McpMemoryServer:
    """提供可嵌入任意 MCP 传输层的最小记忆工具集。"""

    _TOOLS = ("memory_recall", "memory_save", "memory_forget", "memory_explain")

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
            return self._explain(connection, arguments)

    def _save(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """原子保存显式记忆事件并创建提取任务。"""
        text = str(arguments.get("text") or arguments.get("content") or "")
        if not text:
            raise ValueError("text or content is required")
        result = IngestService(connection, self.embedder).save_explicit_memory(
            text,
            str(arguments.get("subject", "用户")),
            str(arguments.get("predicate", "explicit_memory")),
            arguments.get("qualifiers") or {},
        )
        return {**result, "created": True}

    def _recall(self, connection: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过共享召回服务执行混合召回。"""
        query = str(arguments.get("query", ""))
        limit = int(arguments.get("limit", 20))
        return RecallService(connection, self.embedder, self.reranker).recall(
            query,
            limit,
            arguments.get("as_of"),
            arguments.get("intent"),
            arguments.get("known_as_of"),
            arguments.get("query_id"),
            namespace=str(arguments.get("namespace", "default")),
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
from hl_mem import components
