"""LLM extraction 前的确定性、可解释预筛。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

RULE_VERSION = "deterministic-v1"

_DURABLE_SIGNAL = re.compile(
    r"(?:\bremember\b|\bprefer(?:s|red|ence)?\b|\brequire(?:s|d|ment)?\b|\bmust\b|\balways\b|\bnever\b|"
    r"记住|偏好|要求|必须|始终|永远不要|以后都)",
    re.IGNORECASE,
)
_RUNTIME_NOTICE = re.compile(
    r"^\s*\[IMPORTANT:\s*(?:Background process|Tool)\b",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_CONTROL_PREFIX = re.compile(
    r"^\s*\[(?:process|clarify|terminal)\]\s+|"
    r"^\s*\[(?:write_file|patch)\]\s+(?:wrote|replace)\b|"
    r"^\s*\[execute_code\].*\(\d+\s+lines?\s+output\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TRANSIENT_TOOL_RESULT = re.compile(
    r"(?:\[Command timed out after\b|Tool loop warning:|same_tool_failure_warning|"
    r'"status"\s*:\s*"(?:killed|cancelled)"|'
    r'"output"\s*:\s*"Background process started"|'
    r'"completion_reason"\s*:\s*"(?:killed|cancelled)")',
    re.IGNORECASE,
)
_ASSISTANT_ACTION = re.compile(
    r"(?:\b(?:let me|i(?:'ll| will)|next|now|checking|waiting|still running)\b|"
    r"让我|我来|接下来|现在|先|再|继续|跑一下|检查|确认|等待|开始|完成后|重启|提交|"
    r"修复|实现|测试|审查|还在跑|在跑|等它|"
    r"(?:waiting for|still running|check(?:ing)?|verify(?:ing)?|run the tests?|"
    r"等它|还在跑|检查一下|验证一下|跑一下|继续处理))",
    re.IGNORECASE,
)
_OPERATIONAL_STATUS_QUERY = re.compile(
    r"(?:\b(?:is|are|did|does|has|have)\b.*\b(?:done|running|working|normal|latest|deployed|pushed|"
    r"restarted?)\b|"
    r"现在.*(?:完成|正常|运行|生效|推送|重启)|(?:完成|正常|运行|生效|推送|重启).*(?:吗|没|了吧)|"
    r"(?:需要|要不要).*(?:重启|重新开会话)|(?:test|tests?)\s+passed)",
    re.IGNORECASE,
)
_OPERATIONAL_ACTION_REQUEST = re.compile(
    r"(?:^\s*(?:please\s+)?(?:check|review|verify|restart|run|test)\b|"
    r"^\s*(?:请|你可以|你让|再让|帮我).*(?:检查|看看|审查|验证|重启|运行|测试))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PreFilterDecision:
    """描述一次预筛是否应继续调用 extraction LLM。"""

    should_extract: bool
    reason: str


class ExtractionPreFilter:
    """用通用控制流信号跳过明显不含持久事实的事件。"""

    rule_version = RULE_VERSION

    def evaluate(self, event: dict[str, Any], content: dict[str, Any] | str) -> PreFilterDecision:
        """评估事件；无法明确拒绝时一律允许正常 extraction。"""
        if event.get("event_type") == "explicit_memory":
            return PreFilterDecision(True, "explicit_memory")
        if isinstance(content, dict) and content.get("images"):
            return PreFilterDecision(True, "image_content")

        text = self._text(content).strip()
        actor_type = str(event.get("actor_type", ""))
        if actor_type != "tool" and _DURABLE_SIGNAL.search(text):
            return PreFilterDecision(True, "durable_signal")
        if _RUNTIME_NOTICE.search(text):
            return PreFilterDecision(False, "runtime_notice")

        if actor_type == "tool":
            if _TOOL_CONTROL_PREFIX.search(text):
                return PreFilterDecision(False, "tool_control_frame")
            if self._is_transient_tool_result(text):
                return PreFilterDecision(False, "transient_tool_result")
            if self._is_transient_tool_error(text):
                return PreFilterDecision(False, "transient_tool_error")

        if actor_type == "assistant" and len(text) <= 200 and _ASSISTANT_ACTION.search(text):
            return PreFilterDecision(False, "assistant_action_narration")
        if actor_type == "user" and len(text) <= 80 and _OPERATIONAL_STATUS_QUERY.search(text):
            return PreFilterDecision(False, "operational_status_query")
        if actor_type == "user" and len(text) <= 80 and _OPERATIONAL_ACTION_REQUEST.search(text):
            return PreFilterDecision(False, "operational_action_request")
        return PreFilterDecision(True, "eligible")

    @staticmethod
    def _text(content: dict[str, Any] | str) -> str:
        if isinstance(content, str):
            return content
        return "\n".join(
            str(value)
            for key in ("text", "output", "stdout")
            if (value := content.get(key)) is not None
        )

    @staticmethod
    def _is_transient_tool_result(text: str) -> bool:
        if _TRANSIENT_TOOL_RESULT.search(text):
            return True
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("status") in {"killed", "cancelled"} or payload.get("output") == "Background process started"

    @staticmethod
    def _is_transient_tool_error(text: str) -> bool:
        if len(text) > 2_000:
            return False
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict) or not payload.get("error"):
            return False
        return payload.get("success") is False or payload.get("status") == "error"
