"""提取后 claim 蕴含验证器，仅提供审计信号。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)

from .extractors import ExtractedClaim

SupportLabel = Literal[
    "entailed",
    "partially_entailed",
    "contradicted",
    "unsupported",
]

_SUPPORT_LABELS = {
    "entailed",
    "partially_entailed",
    "contradicted",
    "unsupported",
}

_SYSTEM_PROMPT = """你是事实蕴含审计器。逐条判断候选 claim 是否被 source_text 支持，不判断该事实是否值得记忆。
- entailed：原文直接陈述或无歧义蕴含 claim 的完整原子命题。
- partially_entailed：仅部分细节有支持，或主体、数值、范围、时间、限定条件不能完全确认。
- contradicted：原文明示与 claim 相反，或主体、极性、数值、版本、路径、时间发生冲突。
- unsupported：原文既未支持也未反驳，claim 属于补充、猜测或幻觉。
必须为每个 claim_index 返回且只返回一条结果。不得把背景常识当作 source_text 的证据。
输出对象必须是 {"results":[{"claim_index":0,"support_label":"entailed","rationale":"简短理由"}]} 的结构；
support_label 按实际判断替换为上述四个枚举之一。仅输出符合 schema 的 JSON。"""


@dataclass(frozen=True)
class EntailmentResult:
    """单条候选 claim 的蕴含审计结果。"""

    support_label: SupportLabel
    rationale: str


class EntailmentVerifier:
    """批量验证提取的 claim 是否被原文支持（audit-only）。"""

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        structured_mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
    ) -> None:
        self.llm_client = llm_client
        self.structured_mode = structured_mode
        self.last_usage_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.last_call_count = 0

    def verify_batch(
        self,
        claims: list[ExtractedClaim],
        source_text: str,
    ) -> list[EntailmentResult]:
        """在一次 LLM 调用中验证一个 chunk 的所有候选 claim。"""
        self.last_usage_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self.last_call_count = 0
        if not claims:
            return []

        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "source_text": source_text,
                            "claims": [
                                {
                                    "claim_index": index,
                                    "subject": claim.subject,
                                    "predicate": claim.predicate,
                                    "value": claim.value,
                                    "qualifiers": claim.qualifiers or {},
                                    "scope": claim.scope,
                                }
                                for index, claim in enumerate(claims)
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ],
            structured_output=StructuredOutputSpec(
                name="entailment_verification",
                schema=self._response_schema(len(claims)),
                preferred_mode=self.structured_mode,
            ),
        )
        self.last_call_count = 1
        response = self.llm_client.complete(request)
        self.last_usage_tokens = response.usage_total_tokens
        self.last_input_tokens = response.input_tokens or 0
        self.last_output_tokens = response.output_tokens or 0
        if response.finish_reason in {"length", "max_tokens"}:
            raise ValueError("entailment verification output was truncated")

        payload = response.content if isinstance(response.content, dict) else json.loads(response.content)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("entailment verification response must contain a results array")
        raw_results = payload["results"]
        if len(raw_results) != len(claims):
            raise ValueError("entailment verification must return exactly one result per claim")

        ordered: list[EntailmentResult | None] = [None] * len(claims)
        for item in raw_results:
            if not isinstance(item, dict):
                raise ValueError("entailment verification result must be an object")
            claim_index = item.get("claim_index")
            if type(claim_index) is not int or not 0 <= claim_index < len(claims):
                raise ValueError(f"invalid entailment claim_index: {claim_index!r}")
            if ordered[claim_index] is not None:
                raise ValueError(f"duplicate entailment claim_index: {claim_index}")
            support_label = str(item.get("support_label", ""))
            if support_label not in _SUPPORT_LABELS:
                raise ValueError(f"invalid entailment support_label: {support_label!r}")
            rationale = item.get("rationale")
            if not isinstance(rationale, str):
                raise ValueError("entailment rationale must be a string")
            ordered[claim_index] = EntailmentResult(
                cast(SupportLabel, support_label),
                rationale[:512],
            )
        if any(item is None for item in ordered):
            raise ValueError("entailment verification must return exactly one result per claim")
        return [cast(EntailmentResult, item) for item in ordered]

    @staticmethod
    def _response_schema(claim_count: int) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "minItems": claim_count,
                    "maxItems": claim_count,
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_index": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": claim_count - 1,
                            },
                            "support_label": {
                                "type": "string",
                                "enum": sorted(_SUPPORT_LABELS),
                            },
                            "rationale": {"type": "string", "maxLength": 512},
                        },
                        "required": ["claim_index", "support_label", "rationale"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["results"],
            "additionalProperties": False,
        }
