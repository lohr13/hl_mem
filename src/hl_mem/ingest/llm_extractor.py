from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from .extractors import ExtractedClaim

SYSTEM_PROMPT = """你是长期记忆事实提取器。只提取用户值得长期记住的原子事实；忽略闲聊、寒暄和临时信息。只提取事实，不判断是否与已有记忆冲突。输出一个 JSON 对象，包含 claims、entities、should_memorize、sensitivity。每个 claim 包含 subject、predicate、value、qualifiers、confidence、volatility、reason。volatility 只能是 ephemeral（实时状态或临时数据）或 stable（偏好、配置和事实）。
value 必须保持用户使用的原始语言：中文原文输出中文值，英文原文输出英文值，不要翻译。保留原文中的精确数字和日期，不得模糊化或改写。
结合事件上下文中的 occurred_at 解析“今天”“明天”“下周”等相对时间，并在事实中输出对应的绝对日期。
predicate 只能是以下标准值之一：偏好（喜欢或不喜欢的事物）、使用（工具、数据库、操作系统等技术选择）、状态（当前服务或运行状态）、身份（用户名、角色、联系方式）、配置（端口、路径、参数）、计划（计划事项、截止日期）、事实（其他客观事实）。
subject 默认为“用户”；明确提到项目名或服务名时使用该名称。代词（他、她、它、那个）必须结合上下文替换为具体名称；不要在事实中保留代词。
文本包含“改用”“换成”“现在用”“不用了”“改为”等变更信号时，在 qualifiers 中加入 \"change\": true。
不要输出 JSON 以外的解释。"""

ALIASES = {"pg": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}
PREDICATE_NORMALIZE = {
    "prefers": "偏好", "preference": "偏好", "偏好": "偏好", "喜欢": "偏好",
    "uses": "使用", "use": "使用", "使用": "使用", "用": "使用",
    "status": "状态", "状态": "状态", "identity": "身份", "身份": "身份",
    "config": "配置", "配置": "配置", "plan": "计划", "计划": "计划",
    "fact": "事实", "事实": "事实",
}


class LLMExtractor:
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.last_usage_tokens = 0

    def extract(
        self, content: dict[str, Any] | str, event_context: dict[str, Any] | None = None
    ) -> list[ExtractedClaim]:
        self.last_usage_tokens = 0
        body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        event_context = event_context or {}
        context = json.dumps(event_context, ensure_ascii=False)
        occurred_at = event_context.get("occurred_at", "未知")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"事件发生时间 occurred_at：{occurred_at}\n事件上下文：{context}\n对话内容：{body}"},
            ],
            "response_format": {"type": "json_object"},
        }
        response = self._post(payload)
        self.last_usage_tokens = int(response.get("usage", {}).get("total_tokens", 0))
        raw = response["choices"][0]["message"]["content"]
        result = self._parse_json(raw)
        if not result.get("should_memorize", True):
            return []
        claims = result.get("claims", [])
        if not isinstance(claims, list):
            raise ValueError("LLM response claims must be a list")
        return [self._claim(item) for item in claims if isinstance(item, dict)]

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=30.0
                )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        text = str(raw).strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("LLM response does not contain valid JSON") from error
            value = json.loads(match.group())
        if not isinstance(value, dict):
            raise ValueError("LLM response must be a JSON object")
        return value

    @staticmethod
    def _claim(item: dict[str, Any]) -> ExtractedClaim:
        value = str(item.get("value", "")).strip()
        value = ALIASES.get(value.casefold(), value)
        predicate = str(item.get("predicate", "事实")).strip()
        predicate = PREDICATE_NORMALIZE.get(predicate.casefold(), predicate)
        volatility = item.get("volatility", "stable")
        return ExtractedClaim(
            predicate=predicate, value=value,
            confidence=float(item.get("confidence", 0.5)),
            volatility=volatility if volatility in {"stable", "ephemeral"} else "stable",
            subject=str(item.get("subject", "用户")), qualifiers=item.get("qualifiers") or {},
            reason=str(item.get("reason", "")),
        )
