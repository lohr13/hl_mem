from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from .extractors import ExtractedClaim

SYSTEM_PROMPT = """你是长期记忆事实提取器。只提取用户值得长期记住的原子事实；忽略闲聊、寒暄和临时信息。
输出一个 JSON 对象，包含 claims、entities、should_memorize、sensitivity。每个 claim 包含 subject、predicate、
value、qualifiers、confidence、volatility、reason。volatility 只能是 ephemeral（实时状态或临时数据）或
stable（偏好、配置和事实）。不要输出 JSON 以外的解释。"""

ALIASES = {"pg": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}


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
        context = json.dumps(event_context or {}, ensure_ascii=False)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"事件上下文：{context}\n对话内容：{body}"},
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
        volatility = item.get("volatility", "stable")
        return ExtractedClaim(
            predicate=str(item.get("predicate", "fact")), value=value,
            confidence=float(item.get("confidence", 0.5)),
            volatility=volatility if volatility in {"stable", "ephemeral"} else "stable",
            subject=str(item.get("subject", "用户")), qualifiers=item.get("qualifiers") or {},
            reason=str(item.get("reason", "")),
        )
