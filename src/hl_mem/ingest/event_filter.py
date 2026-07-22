from __future__ import annotations

import json
import re
from typing import Any


class EventFilter:
    """Cheap rules that keep low-value events away from the extractor."""

    acknowledgements = re.compile(
        r"^[\s，。！!,.]*(好的|好|明白了|明白|收到|了解|知道了|可以|没问题|ok|okay)[\s，。！!,.]*$",
        re.IGNORECASE,
    )

    def should_extract(self, event: dict[str, Any]) -> tuple[bool, str]:
        if event.get("event_type") == "explicit_memory":
            return True, "explicit_memory"
        content = event.get("content", event.get("content_json", {}))
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                content = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        text = self._text(content).strip()
        if event.get("actor_type") == "assistant" and self.acknowledgements.fullmatch(text):
            return False, "acknowledgement"
        if len(text) < 5:
            return False, "too_short"
        if event.get("event_type") == "tool_result" and self._is_raw_output(content, text):
            return False, "raw_tool_output"
        if event.get("actor_type") == "assistant":
            if self._is_status_report(text):
                return False, "status_report"
        return True, "eligible"

    @staticmethod
    def _text(content: Any) -> str:
        if isinstance(content, dict):
            return str(content.get("text", content.get("output", content.get("stdout", ""))))
        return str(content)

    @staticmethod
    def _is_raw_output(content: Any, text: str) -> bool:
        if isinstance(content, dict) and set(content) - {"text", "output", "stdout", "stderr", "exit_code"}:
            return False
        return bool(text)  # unstructured tool text is raw output by definition

    @staticmethod
    def _is_status_report(text: str) -> bool:
        """检测 assistant 的纯状态汇报消息（无用户价值的事实）。"""
        status_patterns = [
            "healthz", "服务运行中", "服务健康", "服务状态",
            "git commit", "测试通过", "测试全绿", "passed",
            "迁移完成", "migration", "已推送",
        ]
        text_lower = text.lower()
        # 只过滤短消息（<200字符），长消息可能包含有价值内容
        if len(text) > 200:
            return False
        return any(p.lower() in text_lower for p in status_patterns)
