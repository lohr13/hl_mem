"""Hermes 消息与 HL-Mem Episode/Trace 请求之间的映射。"""

from __future__ import annotations

from typing import Any


class EpisodeMapper:
    """从 Hermes 消息推导 Episode 与 Trace 所需的稳定字段。"""

    @staticmethod
    def tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        """提取结构化或兼容格式的工具调用。"""
        structured: list[dict[str, str]] = []
        for message in messages:
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                structured.append({"id": str(call.get("id", "")), "action": str(function.get("name") or "tool")})
        if structured:
            return structured
        return [
            {"id": str(message.get("tool_call_id", index)), "action": str(message.get("name") or "tool")}
            for index, message in enumerate(messages)
            if message.get("role") == "tool"
        ]

    @staticmethod
    def task_type(actions: list[str]) -> str:
        """根据工具名称推导 Episode 任务类型。"""
        lowered = [action.lower() for action in actions]
        if any(any(marker in action for marker in ("terminal", "read_file", "patch")) for action in lowered):
            return "coding"
        if any("web_search" in action for action in lowered):
            return "research"
        return "general"

    @staticmethod
    def error_signature(observation: str | None) -> str | None:
        """从工具观察中提取有界错误签名。"""
        if observation and any(marker in observation.lower() for marker in ("error", "failed", "exception")):
            return observation[:500]
        return None
