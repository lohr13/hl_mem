"""跨主体 Claim 的 LLM 去重判定器。"""

from __future__ import annotations

import json
from typing import Any

from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)


class DedupJudge:
    """使用 LLM 判断两个跨主体 Claim 是否表达同一事实。"""

    _FIELDS = ("subject_entity_id", "predicate", "value", "qualifiers")

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm_client = llm_client

    def judge(
        self, left: dict[str, Any], right: dict[str, Any]
    ) -> tuple[str, float, str]:
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
                            "distinct 表示不同事实；无法可靠判断时返回 uncertain。数字、端口、版本、路径、"
                            "日期或否定含义存在差异时必须返回 distinct。仅输出符合 schema 的 JSON。"
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
        data = (
            response.content
            if isinstance(response.content, dict)
            else json.loads(response.content)
        )
        decision = str(data.get("decision", ""))
        if decision not in {"equivalent", "distinct", "uncertain"}:
            raise ValueError(f"invalid dedup decision: {decision}")
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
        return decision, confidence, str(data.get("reason", ""))[:512]
