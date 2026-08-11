"""跨主体 Claim 的 LLM 去重判定器。"""

from __future__ import annotations

import json
import math
from typing import Any

from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)


class DedupJudge:
    """使用 LLM 判断两个 Claim 是否表达同一事实。"""

    _FIELDS = (
        "subject_entity_id",
        "predicate",
        "value",
        "canonical_slot",
        "canonical_attribute",
        "qualifiers",
        "valid_from",
        "valid_to",
    )

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def judge(self, left: dict[str, Any], right: dict[str, Any]) -> tuple[str, float, str]:
        """返回判定、置信度和简短理由。"""
        facts = {
            "left": {field: left.get(field) for field in self._FIELDS},
            "right": {field: right.get(field) for field in self._FIELDS},
        }
        response = self.llm_client.complete(
            LLMRequest(
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            "判断两条 claim 是否表达同一事实。equivalent 表示主体写法虽不同但事实相同；"
                            "distinct 表示不同事实；无法可靠判断时返回 uncertain。配置键、task、数字、端口、"
                            "版本、路径、有效时间或肯定/否定极性存在差异时必须返回 distinct。"
                            "HTTP_PROXY 与 HTTPS_PROXY 等不同标识符不是同一事实。"
                            "仅输出一个符合 schema 的 JSON object，且必须包含 decision、confidence、reason；"
                            '例如 {"decision":"uncertain","confidence":0.0,"reason":"insufficient evidence"}。'
                        ),
                    ),
                    LLMMessage(
                        role="user",
                        content=json.dumps(facts, ensure_ascii=False, sort_keys=True),
                    ),
                ],
                structured_output=StructuredOutputSpec(
                    name="cross_subject_dedup_decision",
                    schema={
                        "type": "object",
                        "properties": {
                            "decision": {
                                "type": "string",
                                "enum": ["equivalent", "distinct", "uncertain"],
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["decision", "confidence", "reason"],
                        "additionalProperties": False,
                    },
                    preferred_mode=StructuredOutputMode.JSON_SCHEMA,
                ),
            )
        )
        try:
            data = response.content if isinstance(response.content, dict) else json.loads(response.content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return "uncertain", 0.0, "invalid_output:json"
        if not isinstance(data, dict):
            return "uncertain", 0.0, "invalid_output:object"
        raw_decision = data.get("decision")
        if not isinstance(raw_decision, str) or raw_decision not in {"equivalent", "distinct", "uncertain"}:
            return "uncertain", 0.0, "invalid_output:decision"
        raw_confidence = data.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            return "uncertain", 0.0, "invalid_output:confidence"
        confidence = float(raw_confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return "uncertain", 0.0, "invalid_output:confidence"
        reason = data.get("reason")
        if not isinstance(reason, str):
            return "uncertain", 0.0, "invalid_output:reason"
        return raw_decision, confidence, reason[:512]
