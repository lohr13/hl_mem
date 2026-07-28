"""LLM extraction 前的确定性、可解释预筛。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

RULE_VERSION = "deterministic-v2"
ASSISTANT_ACTION_MAX_CHARS = 60

_DURABLE_SIGNAL = re.compile(
    r"(?:\bremember\b|\bprefer(?:s|red|ence)?\b|\brequire(?:s|d|ment)?\b|\bmust\b|\balways\b|\bnever\b|"
    r"记住|偏好|要求|必须|始终|永远不要|以后都)",
    re.IGNORECASE,
)
_DURABLE_DIAGNOSTIC_SIGNAL = re.compile(
    r"(?:"
    r"\bport\s+\d{2,5}\b|\b(?:provider|model|version)\s*(?:is|=|:)\s*\S+|"
    r"(?-i:\b[A-Z][A-Z0-9_]{2,}\b)\s*(?:is|=|:)\s*\S+|"
    r"(?:database|config(?:uration)?|working)?\s*(?:file\s+)?path\s*(?:is|=|:)\s*\S+|"
    r"(?:[A-Za-z]:[\\/]|/(?:etc|opt|srv|var|home|workspace)/)\S+|"
    r"(?:root cause|caused by|because|根因|原因是|端口|路径|版本|环境变量|provider)"
    r")",
    re.IGNORECASE,
)
_DURABLE_TOOL_MEMORY_SIGNAL = re.compile(
    r"(?:\bremember\b|\bprefer(?:s|red|ence)?\b|\bmust\b|\balways\b|\bnever\b|" r"记住|偏好|必须|始终|永远不要|以后都)",
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
_TOOL_WRAPPER_KEEP_SIGNAL = re.compile(
    r"(?:--version\b|\b(?:pwd|cwd)\b|working\s+director(?:y|ies)|工作目录)",
    re.IGNORECASE,
)
_ASSISTANT_ACTION = re.compile(
    r"^\s*(?:"
    r"(?:let me|i(?:'ll| will)|next(?:,\s*)?(?:i(?:'ll| will)\s+)?|"
    r"now(?:,\s*)?(?:i(?:'ll| will)\s+)?)"
    r"\s+(?:check|verify|run|test|review|restart|wait|continue|fix|implement|submit)\b[^.!?\n]*[:：.]?|"
    r"(?:checking|waiting(?:\s+for)?|still running|running the tests?|continuing)\b[^.!?\n]*[:：.]?|"
    r"(?:让我|我来|我(?:先|再|现在|接下来)?|先|再|接下来|现在)"
    r"(?:检查|确认|验证|跑|运行|测试|重启|提交|修复|实现|审查|继续处理|等|等待)"
    r"[^。！？\n]*[:：。]?|"
    r"(?:继续等待|等它(?:完成)?|还在跑|正在(?:检查|确认|验证|运行|测试))[^。！？\n]*[:：。]?|"
    r"(?:检查|确认|验证|测试|运行|跑一下|提交|重启)(?:一下)?[^。！？\n]{0,24}[:：]"
    r")\s*$",
    re.IGNORECASE,
)
_OPERATIONAL_STATUS_QUERY = re.compile(
    r"(?:\b(?:is|are|did|does|has|have)\b.*\b(?:done|running|working|normal|latest|deployed|pushed|"
    r"restarted?)\b|"
    r"现在.*(?:完成|正常|运行|生效|推送|重启)|(?:完成|正常|运行|生效|推送|重启).*(?:吗|没|了吧)|"
    r"(?:需要|要不要).*(?:重启|重新开会话)|(?:test|tests?)\s+passed)",
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
            if _TOOL_CONTROL_PREFIX.search(text) and not self._contains_durable_tool_signal(text):
                return PreFilterDecision(False, "tool_control_frame")
            if self._is_transient_tool_result(text):
                return PreFilterDecision(False, "transient_tool_result")
            if self._is_transient_tool_error(text):
                return PreFilterDecision(False, "transient_tool_error")

        if actor_type == "assistant" and len(text) <= ASSISTANT_ACTION_MAX_CHARS and _ASSISTANT_ACTION.fullmatch(text):
            return PreFilterDecision(False, "assistant_action_narration")
        if actor_type == "user" and len(text) <= 80 and _OPERATIONAL_STATUS_QUERY.search(text):
            return PreFilterDecision(False, "operational_status_query")
        return PreFilterDecision(True, "eligible")

    @staticmethod
    def _text(content: dict[str, Any] | str) -> str:
        if isinstance(content, str):
            return content
        return "\n".join(str(value) for key in ("text", "output", "stdout") if (value := content.get(key)) is not None)

    @staticmethod
    def _contains_durable_tool_signal(text: str) -> bool:
        """判断 tool 正文是否包含值得提取的持久配置或诊断证据。"""
        return bool(
            _DURABLE_TOOL_MEMORY_SIGNAL.search(text)
            or _DURABLE_DIAGNOSTIC_SIGNAL.search(text)
            or _TOOL_WRAPPER_KEEP_SIGNAL.search(text)
        )

    @staticmethod
    def _is_transient_tool_result(text: str) -> bool:
        if re.fullmatch(
            r"\s*(?:\[Command timed out after\b[^\]\r\n]*\]|Tool loop warning:.*|"
            r"same_tool_failure_warning|Background process started)\s*",
            text,
            re.IGNORECASE,
        ):
            return True
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        durable_text = "\n".join(
            str(payload[key])
            for key in ("error", "output", "stdout")
            if payload.get(key) is not None and payload.get(key) != ""
        )
        if ExtractionPreFilter._contains_durable_tool_signal(durable_text):
            return False
        status = payload.get("status")
        completion_reason = payload.get("completion_reason")
        output = payload.get("output")
        if (status in {"killed", "cancelled"} or completion_reason in {"killed", "cancelled"}) and output in {None, ""}:
            return True
        if not isinstance(output, str):
            return False
        return bool(
            re.fullmatch(
                r"\s*(?:Background process started|\[Command timed out after\b[^\]\r\n]*\])\s*",
                output,
                re.IGNORECASE,
            )
        )

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
        durable_text = "\n".join(
            str(payload[key])
            for key in ("error", "output", "stdout")
            if payload.get(key) is not None and payload.get(key) != ""
        )
        if ExtractionPreFilter._contains_durable_tool_signal(durable_text):
            return False
        return payload.get("success") is False or payload.get("status") == "error"
