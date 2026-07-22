"""Hermes 的 HL-Mem 记忆提供器插件。"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

from agent.memory_provider import MemoryProvider


class HlMemProvider(MemoryProvider):
    """通过标准库 HTTP 接口连接 HL-Mem 服务。"""

    def __init__(self) -> None:
        """初始化配置、预取缓存与熔断器状态。"""
        self._base_url = os.getenv("HL_MEM_URL", "http://localhost:8200").rstrip("/")
        self._timeout = float(os.getenv("HL_MEM_TIMEOUT", "10"))
        self._failure_count = 0
        self._failure_threshold = 5
        self._circuit_open_until = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cache: dict[str, str] = {}
        self._session_id = ""
        self._hermes_home = ""

    @property
    def name(self) -> str:
        """返回提供器名称。"""
        return "hl_mem"

    def is_available(self) -> bool:
        """返回插件是否由环境变量启用。"""
        return os.getenv("HL_MEM_ENABLED", "true").lower() != "false"

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """保存当前 Hermes 会话信息。"""
        self._session_id = session_id
        self._hermes_home = str(kwargs.get("hermes_home") or os.getenv("HERMES_HOME", ""))

    def get_tool_schemas(self) -> list[Any]:
        """返回插件提供的工具定义。"""
        return []

    def system_prompt_block(self) -> str:
        """返回注入系统提示词的记忆状态说明。"""
        return "# hl_mem Memory\nActive. Relevant memories injected into context."

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> str:
        """经统一 HTTP 入口发送 JSON 请求，失败时静默降级并更新熔断器。"""
        if not self._can_call():
            return ""
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8")
            with self._lock:
                self._failure_count = 0
            return body
        except Exception:
            with self._lock:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._circuit_open_until = time.monotonic() + 60.0
                    self._failure_count = 0
            return ""

    def _can_call(self) -> bool:
        """返回熔断窗口是否允许发起请求。"""
        return time.monotonic() >= self._circuit_open_until

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """同步一轮对话，并在存在多个工具调用时记录 Episode。"""
        active_session = session_id or self._session_id
        self._request("POST", "/v1/events", self._event_payload("user", user_content, active_session))
        self._request("POST", "/v1/events", self._event_payload("assistant", assistant_content, active_session))
        if messages and len(self._extract_tool_calls(messages)) >= 2:
            self._sync_episode(messages, active_session)

    def _sync_episode(self, messages: list[dict[str, Any]], session_id: str) -> None:
        """从消息提取工具轨迹并写入 Episode、Trace 与执行结果。"""
        tool_calls = self._extract_tool_calls(messages)
        if len(tool_calls) < 2:
            return
        observations = {
            str(message.get("tool_call_id", "")): str(message.get("content", ""))
            for message in messages
            if message.get("role") == "tool"
        }
        goal_message = next((message for message in messages if message.get("role") == "user"), {})
        goal = str(goal_message.get("content") or "Complete tool-assisted task")
        response_body = self._request(
            "POST",
            "/v1/episodes",
            {
                "goal": goal,
                "session_id": session_id or None,
                "task_type": self._detect_task_type([call["action"] for call in tool_calls]),
            },
        )
        try:
            episode_id = str(json.loads(response_body)["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return

        has_error = False
        for call in tool_calls:
            observation = observations.get(call["id"])
            error_signature = self._detect_error(observation)
            has_error = has_error or error_signature is not None
            self._request(
                "POST",
                f"/v1/episodes/{episode_id}/traces",
                {
                    "action": call["action"],
                    "observation": observation,
                    "error_signature": error_signature,
                    "value": 0.0 if error_signature else 1.0,
                },
            )

        goal_index = messages.index(goal_message) if goal_message else -1
        final_answer = any(
            message.get("role") == "assistant" and message.get("content") for message in messages[goal_index + 1 :]
        )
        status = "failed" if has_error and not final_answer else "success"
        reward = 0.2 if status == "failed" else (0.5 if has_error else 0.8)
        self._request(
            "PATCH",
            f"/v1/episodes/{episode_id}",
            {
                "status": status,
                "reward": reward,
                "outcome_summary": "turn completed" if final_answer else status,
            },
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """在后台线程中预取相关记忆。"""
        active_session = session_id or self._session_id

        def fetch() -> None:
            body = self._request("POST", "/v1/recall", {"query": query, "session_id": active_session or None})
            rendered = self._render_recall(body)
            with self._lock:
                self._cache[active_session] = rendered

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=fetch, name="hl-mem-prefetch", daemon=True)
            self._thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """返回当前会话已经缓存的预取结果。"""
        del query
        active_session = session_id or self._session_id
        with self._lock:
            return self._cache.get(active_session, "")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """将 Hermes 的显式记忆写入镜像到 HL-Mem。"""
        qualifiers: dict[str, Any] = {"action": action, "target": target}
        if metadata:
            qualifiers["metadata"] = metadata
        self._request("POST", "/v1/memories", {"text": content, "qualifiers": qualifiers})

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """在上下文压缩前同步即将被压缩的消息。"""
        for message in messages:
            self._request(
                "POST",
                "/v1/events",
                self._event_payload(
                    str(message.get("role") or "user"),
                    str(message.get("content") or ""),
                    str(message.get("session_id") or self._session_id),
                ),
            )
        return ""

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """记录委派任务及其子代理结果。"""
        del kwargs
        qualifiers = {"child_session_id": child_session_id} if child_session_id else None
        self._request("POST", "/v1/events", self._event_payload("user", task, self._session_id, qualifiers))
        self._request("POST", "/v1/events", self._event_payload("assistant", result, self._session_id, qualifiers))

    def on_session_end(self, **kwargs: Any) -> None:
        """处理 Hermes 会话结束钩子。"""
        del kwargs

    def shutdown(self) -> None:
        """等待正在运行的预取线程短暂收尾。"""
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=self._timeout)

    @staticmethod
    def _extract_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        """从 OpenAI 结构或 tool 角色消息中提取工具调用。"""
        structured: list[dict[str, str]] = []
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                structured.append({"id": str(call.get("id", "")), "action": str(function.get("name") or "tool")})
        if structured:
            return structured
        for index, message in enumerate(messages):
            if message.get("role") == "tool":
                structured.append(
                    {"id": str(message.get("tool_call_id", index)), "action": str(message.get("name") or "tool")}
                )
        return structured

    @staticmethod
    def _detect_task_type(actions: list[str]) -> str:
        """依据工具名称判断 Episode 的任务类型。"""
        lowered = [action.lower() for action in actions]
        coding_markers = ("terminal", "read_file", "patch", "write_file", "search_files")
        if any(any(marker in action for marker in coding_markers) for action in lowered):
            return "coding"
        if any("web_search" in action or "web_extract" in action for action in lowered):
            return "research"
        return "general"

    @staticmethod
    def _detect_error(observation: str | None) -> str | None:
        """从工具观察文本中提取错误签名。"""
        markers = ("error", "failed", "exception", "traceback")
        if observation and any(marker in observation.lower() for marker in markers):
            return observation[:500]
        return None

    @staticmethod
    def _event_payload(
        role: str,
        content: str,
        session_id: str,
        qualifiers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建 HL-Mem 事件请求体。"""
        payload: dict[str, Any] = {
            "event_type": "message",
            "actor_type": role,
            "content": {"text": content},
            "session_id": session_id or None,
        }
        if qualifiers:
            payload["content"]["qualifiers"] = qualifiers
        return payload

    @staticmethod
    def _render_recall(body: str) -> str:
        """将召回 JSON 响应渲染为可注入上下文的文本。"""
        if not body:
            return ""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return ""
        lines = [str(item.get("text", "")) for item in payload.get("results", []) if item.get("text")]
        return "\n".join(lines)


def register(ctx: Any) -> None:
    """向 Hermes 注册 HL-Mem 记忆提供器。"""
    ctx.register_memory_provider(HlMemProvider())
